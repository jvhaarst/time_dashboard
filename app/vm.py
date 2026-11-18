"""Read the NTP/GNSS series out of VictoriaMetrics.

The store this talks to is a second, NTP-only VictoriaMetrics that keeps a year of
clock data (`VMSingle/ntp` in the `monitoring` namespace). It is a ClusterIP with no
route from outside the cluster, which is the point: the browser never queries it, this
process does, and only the numbers below ever leave the cluster.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)

VM_URL = os.environ.get(
    "VM_URL", "http://vmsingle-ntp.monitoring.svc.cluster.local:8429"
).rstrip("/")

# Seconds. Kept well under the readiness probe's timeout so a slow store makes the page
# late rather than making the pod unready.
VM_TIMEOUT = float(os.environ.get("VM_TIMEOUT", "5"))

# One request fetches every instant value the page needs. Series are indexed by
# __name__ afterwards. A regex selector beats 20 round trips, and it beats `a or b`:
# PromQL set operators drop __name__ before matching labels, so `metric_a or metric_b`
# over series that differ ONLY in __name__ silently returns just the left-hand one.
INSTANT_SELECTOR = (
    '{__name__=~"'
    "chrony_up"
    "|chrony_tracking_(stratum|last_offset_seconds|rms_offset_seconds"
    "|root_delay_seconds|root_dispersion_seconds|skew_ppms|frequency_ppms"
    "|update_interval_seconds|system_time_seconds|info)"
    "|chrony_serverstats_ntp_packets_(received|dropped)_total"
    "|chrony_sources_(state_info|stratum|polling_interval_seconds|reachability_ratio"
    "|last_sample_offset_seconds|last_sample_error_margin_seconds|last_sample_age_seconds)"
    "|chrony_sourcestats_stddev"
    "|gpsd_(nSat|uSat|mode|sat_ss|pdop|tdop|hdop|leapseconds)"
    "|ntp_clients_(active|log_entries|total|countries_total)"
    "|node_boot_time_seconds|node_hwmon_temp_celsius"
    '"}'
)

# gpsd reports a numeric constellation id per satellite. The mapping is gpsd's, not
# ours: see the `gnssid` field in gpsd's JSON SKY report.
GNSS_NAMES = {
    "0": "GPS",
    "1": "SBAS",
    "2": "Galileo",
    "3": "BeiDou",
    "4": "IMES",
    "5": "QZSS",
    "6": "GLONASS",
    "7": "NavIC",
}

# gpsd's fix mode.
FIX_MODES = {"0": "unknown", "1": "no fix", "2": "2D fix", "3": "3D fix"}


@dataclass
class Sample:
    """One instant value with its labels."""

    value: float
    labels: dict[str, str] = field(default_factory=dict)


class VictoriaMetricsError(RuntimeError):
    pass


def _query(expr: str) -> list[dict[str, Any]]:
    try:
        r = requests.get(
            f"{VM_URL}/api/v1/query", params={"query": expr}, timeout=VM_TIMEOUT
        )
        r.raise_for_status()
        body = r.json()
    except requests.RequestException as exc:
        raise VictoriaMetricsError(f"querying {VM_URL}: {exc}") from exc
    except ValueError as exc:
        raise VictoriaMetricsError(f"{VM_URL} did not answer JSON: {exc}") from exc
    if body.get("status") != "success":
        raise VictoriaMetricsError(f"{VM_URL} answered {body.get('status')!r}")
    return body.get("data", {}).get("result", [])


def query(expr: str) -> list[Sample]:
    """An arbitrary instant query, as a flat list of samples with their labels."""
    out = []
    for series in _query(expr):
        labels = dict(series.get("metric", {}))
        labels.pop("__name__", None)
        try:
            value = float(series["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        out.append(Sample(value=value, labels=labels))
    return out


def instant() -> dict[str, list[Sample]]:
    """Every series the page needs, grouped by metric name.

    A metric with no samples is simply absent from the result, so every caller has to
    cope with a missing key. That is deliberate: the page should degrade to "unknown"
    for one row rather than fail as a whole.
    """
    out: dict[str, list[Sample]] = {}
    for series in _query(INSTANT_SELECTOR):
        labels = dict(series.get("metric", {}))
        name = labels.pop("__name__", "")
        if not name:
            continue
        try:
            value = float(series["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        out.setdefault(name, []).append(Sample(value=value, labels=labels))
    return out


def query_range(
    expr: str, start: float, end: float, step: int
) -> list[tuple[dict[str, str], list[tuple[float, float]]]]:
    """A range query, as (labels, [(timestamp, value), ...]) per series.

    Mind the point budget: VictoriaMetrics refuses a range query that would exceed
    `-search.maxPointsPerTimeseries` (30,000 by default) with **HTTP 422**, and it does
    so for the whole query rather than truncating. 30 days at a 60 s step is 43,200
    points and fails; 120 s is 21,600 and works. That is why the frequency history uses
    a 120 s base interval.
    """
    try:
        r = requests.get(
            f"{VM_URL}/api/v1/query_range",
            params={
                "query": expr,
                "start": int(start),
                "end": int(end),
                "step": step,
            },
            timeout=VM_TIMEOUT * 4,
        )
        r.raise_for_status()
        body = r.json()
    except requests.RequestException as exc:
        raise VictoriaMetricsError(f"range query against {VM_URL}: {exc}") from exc
    except ValueError as exc:
        raise VictoriaMetricsError(f"{VM_URL} did not answer JSON: {exc}") from exc
    if body.get("status") != "success":
        raise VictoriaMetricsError(
            f"{VM_URL} answered {body.get('status')!r}: {body.get('error')}"
        )
    out = []
    for series in body.get("data", {}).get("result", []):
        labels = dict(series.get("metric", {}))
        labels.pop("__name__", None)
        points = []
        for ts, raw in series.get("values", []):
            try:
                points.append((float(ts), float(raw)))
            except (TypeError, ValueError):
                continue
        out.append((labels, points))
    return out


def scalar(expr: str) -> float | None:
    """The first value of an instant query, or None."""
    result = _query(expr)
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def packets_per_second(window: str = "5m") -> float | None:
    """NTP requests answered per second, averaged over `window`.

    `rate()` over a counter rather than a difference of two instant reads: chronyd's
    counter resets when chronyd restarts, and rate() handles that reset.
    """
    result = _query(
        f"sum(rate(chrony_serverstats_ntp_packets_received_total[{window}]))"
    )
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def one(samples: dict[str, list[Sample]], name: str) -> float | None:
    """The single value of a metric that should have exactly one series."""
    series = samples.get(name)
    if not series:
        return None
    return series[0].value


def labels_of(samples: dict[str, list[Sample]], name: str) -> dict[str, str]:
    series = samples.get(name)
    if not series:
        return {}
    return series[0].labels


def by_label(
    samples: dict[str, list[Sample]], name: str, label: str
) -> dict[str, float]:
    """A metric's values keyed by one of its labels, e.g. ntp_clients_active by window."""
    return {
        s.labels.get(label, ""): s.value
        for s in samples.get(name, [])
        if label in s.labels
    }


def source_offset(samples: dict[str, list[Sample]], source_name: str) -> float | None:
    for s in samples.get("chrony_sources_last_sample_offset_seconds", []):
        if s.labels.get("source_name") == source_name:
            return s.value
    return None


def satellites(samples: dict[str, list[Sample]]) -> list[dict[str, Any]]:
    """Per-constellation satellite counts, seen and used.

    `gpsd_sat_ss` carries one series per satellite in view, labelled with `gnssid`,
    `PRN`, `svid` and `used`. Counting those series per constellation gives "seen", and
    counting the ones whose `used` label is true gives "used".

    Do NOT reach for `gpsd_sat_used` here: despite the name it is a single gauge holding
    the total number of satellites in the fix, with no `gnssid` label at all, so
    `sum by (gnssid) (gpsd_sat_used)` buckets everything under an empty key and every
    per-constellation figure comes out zero. The exporter writes the flag as Python's
    `str(bool)`, so the label values are the strings "True" and "False".
    """
    truthy = {"true", "1", "yes"}
    seen: dict[str, int] = {}
    used: dict[str, int] = {}
    for s in samples.get("gpsd_sat_ss", []):
        gnss = s.labels.get("gnssid", "")
        seen[gnss] = seen.get(gnss, 0) + 1
        if s.labels.get("used", "").strip().lower() in truthy:
            used[gnss] = used.get(gnss, 0) + 1
    rows = [
        {
            "gnssid": gnss,
            "name": GNSS_NAMES.get(gnss, f"gnssid {gnss}"),
            "seen": count,
            "used": used.get(gnss, 0),
        }
        for gnss, count in seen.items()
    ]
    rows.sort(key=lambda r: (-r["used"], -r["seen"], r["name"]))
    return rows


def satellite_detail(samples: dict[str, list[Sample]]) -> list[dict[str, Any]]:
    """One row per satellite in view, sorted by constellation then signal strength.

    `used` comes from the label on `gpsd_sat_ss`, for the reason given in `satellites()`.
    """
    truthy = {"true", "1", "yes"}
    rows = []
    for s in samples.get("gpsd_sat_ss", []):
        gnss = s.labels.get("gnssid", "")
        rows.append(
            {
                "constellation": GNSS_NAMES.get(gnss, f"gnssid {gnss}"),
                "gnssid": gnss,
                "prn": s.labels.get("PRN") or s.labels.get("svid") or "?",
                "ss": s.value,
                "used": s.labels.get("used", "").strip().lower() in truthy,
            }
        )
    rows.sort(key=lambda r: (r["gnssid"], -r["ss"]))
    return rows


def mean_signal(samples: dict[str, list[Sample]], used_only: bool = True) -> float | None:
    """Mean carrier-to-noise of the satellites, which is the usual antenna-health number.

    Over the satellites in the fix by default: the ones the receiver sees but rejects are
    typically low and would drag the figure down without saying anything about the
    antenna.
    """
    truthy = {"true", "1", "yes"}
    values = [
        s.value
        for s in samples.get("gpsd_sat_ss", [])
        if not used_only or s.labels.get("used", "").strip().lower() in truthy
    ]
    if not values:
        return None
    return sum(values) / len(values)
