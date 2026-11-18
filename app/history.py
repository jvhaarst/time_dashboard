"""Everything that needs a range query: offset history, the lock calendar, Allan
deviation, and the per-source comparison.

All of it comes out of the one-year VictoriaMetrics store. Each result is cached for
longer than the live figures are, because none of it changes meaningfully inside a
minute and the 30-day queries are the expensive ones.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

from . import vm

# Selectable history windows. `step` keeps every range inside
# `-search.maxPointsPerTimeseries` (30,000): 30 days at 120 s is 21,600 points, and at
# 60 s it is 43,200 and the query is refused outright with HTTP 422.
RANGES: dict[str, dict[str, int]] = {
    "1h": {"seconds": 3600, "step": 30},
    "24h": {"seconds": 86400, "step": 120},
    "7d": {"seconds": 7 * 86400, "step": 600},
    "30d": {"seconds": 30 * 86400, "step": 1800},
}
DEFAULT_RANGE = "24h"

# The frequency series the Allan deviation is computed from. 120 s is the shortest base
# interval a 30-day query can carry, so it is the shortest tau the plot can show.
ADEV_SECONDS = 30 * 86400
ADEV_TAU0 = 120


#: The characters RE2 treats as special. Everything else is matched literally and must
#: NOT be backslash-escaped.
_RE2_SPECIAL = set(r".+*?()[]{}|^$\\")


def re2_quote(text: str) -> str:
    """Escape a literal for a label regex, the way Go's RE2 accepts.

    `re.escape` cannot be used here. Python escapes every character that is not
    alphanumeric or `_`, which includes `-`, and **RE2 rejects `\\-` as an invalid escape
    sequence**: VictoriaMetrics answers the whole query with HTTP 400. One NTP source on
    this server is `static.ip-031-223-173-226.signet.nl`, so the hyphen is not a corner
    case.
    """
    return "".join("\\" + ch if ch in _RE2_SPECIAL else ch for ch in text)


def label_regex(names: list[str]) -> str:
    """An alternation of literal names, quoted ready to paste into a selector.

    **There are two escaping layers here and getting one of them wrong is silent or
    fatal depending on which.** The inner layer is the RE2 regex: a literal `.` has to
    become `\\.` or it matches any character. The outer layer is MetricsQL's string
    literal, which processes backslash escapes of its own and, unlike Prometheus, is
    strict about them -- a query containing `"chime1\\.surf\\.nl"` is rejected with
    `cannot parse string literal ... invalid syntax`, because `\\.` is not a valid string
    escape. The backslash therefore has to be doubled in the query text so that the
    string literal decodes to the single backslash the regex wants.
    """
    inner = "|".join(re2_quote(name) for name in names)
    return '"' + inner.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class _Entry:
    at: float
    value: object


_cache: dict[str, _Entry] = {}


def cached(key: str, ttl: float, build: Callable[[], object]) -> object:
    entry = _cache.get(key)
    now = time.monotonic()
    if entry is not None and now - entry.at < ttl:
        return entry.value
    value = build()
    _cache[key] = _Entry(at=now, value=value)
    return value


def offset_history(range_key: str) -> dict[str, object]:
    """System-clock offset over a window, as mean with a min-max band.

    Three rollups over the same series rather than raw samples: at 30 days the raw
    series is 86,400 points and a chart cannot show them, while min and max are exactly
    what must not be averaged away -- a single 100 µs excursion is the interesting part
    of an otherwise flat month.
    """
    spec = RANGES.get(range_key, RANGES[DEFAULT_RANGE])
    end = time.time()
    start = end - spec["seconds"]
    step = spec["step"]
    window = f"{step}s"
    series = {}
    for name, fn in (("mean", "avg_over_time"), ("min", "min_over_time"), ("max", "max_over_time")):
        points = vm.query_range(
            f"{fn}(chrony_tracking_last_offset_seconds[{window}])", start, end, step
        )
        series[name] = points[0][1] if points else []
    return {
        "range": range_key,
        "start": start,
        "end": end,
        "step": step,
        "mean": series["mean"],
        "min": series["min"],
        "max": series["max"],
    }


def peer_history(range_key: str, limit: int = 8) -> list[dict[str, object]]:
    """This clock's offset relative to each network source, over a window.

    Only the network sources: the local reference clocks (`PPS`, `GPS`) are what the
    clock is steered by, so plotting them against it compares a thing with itself. The
    sign convention is chrony's -- a positive offset means the source is ahead of this
    clock.
    """
    spec = RANGES.get(range_key, RANGES[DEFAULT_RANGE])
    end = time.time()
    start = end - spec["seconds"]
    step = spec["step"]

    # Which sources are network sources has to be asked of `chrony_sources_state_info`:
    # it is the ONLY series in this family that carries `source_mode` and `source_state`.
    # `chrony_sources_last_sample_offset_seconds` has just `source_name` and
    # `source_address`, so filtering it on `{source_mode="client"}` matches nothing at
    # all and the chart comes out empty with no error anywhere.
    names = [
        str(row["name"])
        for row in sources()
        if row.get("mode") == "client" and row.get("name")
    ]
    if not names:
        return []
    rows = vm.query_range(
        "avg_over_time(chrony_sources_last_sample_offset_seconds"
        f"{{source_name=~{label_regex(names)}}}[{step}s])",
        start,
        end,
        step,
    )
    out = [
        {
            "name": labels.get("source_name", "?"),
            "address": labels.get("source_address", ""),
            "points": points,
        }
        for labels, points in rows
        if points
    ]
    # Which eight, when there are sixteen: the ones chrony measures most consistently,
    # by sourcestats standard deviation. Alphabetical would be worse than arbitrary here
    # -- it picks `chime1` through `chime7`, seven machines in one building, and drops
    # every geographically or administratively distinct source.
    quality = {
        str(row["name"]): row.get("stddev")
        for row in sources()
        if row.get("stddev") is not None
    }
    out.sort(key=lambda r: (quality.get(str(r["name"]), float("inf")), str(r["name"])))
    return out[:limit]


def sources() -> list[dict[str, object]]:
    """The live `chronyc sources` / `sourcestats` view.

    Every figure is read from the exporter's own labels and values rather than
    recomputed. `source_state` is chrony's selection verdict and carries the same
    meaning as the symbol in `chronyc sources`: `sync` is the selected source,
    `candidate` is combined with it, `outlier` is measured but excluded from the
    combination, `unreach` is not currently usable, `falseticker` disagrees with the
    majority and `jittery` is too variable to use.
    """
    samples = vm.instant()
    by_name: dict[str, dict[str, object]] = {}

    def put(metric: str, field: str) -> None:
        for s in samples.get(metric, []):
            name = s.labels.get("source_name")
            if not name:
                continue
            row = by_name.setdefault(
                name,
                {
                    "name": name,
                    "address": s.labels.get("source_address", ""),
                    "mode": s.labels.get("source_mode", ""),
                    "state": "",
                },
            )
            row[field] = s.value

    for s in samples.get("chrony_sources_state_info", []):
        name = s.labels.get("source_name")
        if not name:
            continue
        row = by_name.setdefault(
            name, {"name": name, "address": s.labels.get("source_address", "")}
        )
        row["state"] = s.labels.get("source_state", "")
        row["mode"] = s.labels.get("source_mode", "")
        row["address"] = s.labels.get("source_address", "")

    put("chrony_sources_last_sample_offset_seconds", "offset")
    put("chrony_sources_last_sample_error_margin_seconds", "error")
    put("chrony_sources_stratum", "stratum")
    put("chrony_sources_polling_interval_seconds", "poll")
    put("chrony_sources_reachability_ratio", "reach")
    put("chrony_sourcestats_stddev", "stddev")
    put("chrony_sources_last_sample_age_seconds", "age")

    # Reference clocks first, then by name: the selected source should be at the top,
    # and on a stratum-1 server that is always a reference clock.
    order = {"reference clock": 0}
    return sorted(
        by_name.values(),
        key=lambda r: (order.get(str(r.get("mode", "")), 1), str(r.get("name", ""))),
    )


def lock_calendar(days: int = 30) -> list[dict[str, object]]:
    """Per-day fraction of the day the server was locked to its local reference.

    "Locked" is `chrony_tracking_stratum == 1`, which is true only while a reference
    clock is selected; the moment chronyd falls back to a network source the stratum
    goes to 2 or worse. The fraction is over the samples that exist, so a day with no
    data at all reports `None` rather than 0 -- a gap in monitoring is not an outage.
    """
    end = time.time()
    start = end - days * 86400
    # NOTE THE `:300` -- it is the whole correctness of this chart.
    #
    # `(expr)[1d]` where expr is not a bare metric selector is a *subquery*, and a
    # subquery with no explicit step inherits the outer query's step. This range query
    # steps by a day, so `sum_over_time((stratum == bool 1)[1d])` evaluated the inner
    # expression ONCE per day and summed that single sample, while
    # `count_over_time(stratum[1d])` counted all 2,880 raw samples. The ratio came out
    # as 1/2880 and the calendar drew every day red at 0.036% availability, on a server
    # that had not missed a second.
    #
    # `avg_over_time((...)[1d:300])` evaluates the inner expression every 300 s and
    # averages the 1s and 0s, which is the fraction of the day the server was locked.
    # The resolution of that figure is therefore five minutes: an outage shorter than
    # one sampling interval can fall between two evaluations and go unseen.
    locked = vm.query_range(
        "avg_over_time((chrony_tracking_stratum == bool 1)[1d:300])", start, end, 86400
    )
    total = vm.query_range(
        "count_over_time(chrony_tracking_stratum[1d])", start, end, 86400
    )
    locked_by_ts = dict(locked[0][1]) if locked else {}
    total_by_ts = dict(total[0][1]) if total else {}
    out = []
    for ts in sorted(total_by_ts):
        n = total_by_ts.get(ts, 0.0)
        out.append(
            {
                "timestamp": ts,
                "samples": int(n),
                # A day with no samples reports None, not 0: a gap in monitoring is
                # not an outage and must not be drawn as one.
                "availability": locked_by_ts.get(ts) if n else None,
            }
        )
    return out


def locked_since() -> float | None:
    """When the current unbroken stretch of stratum 1 began.

    Walks back through the stratum series and returns the timestamp just after the last
    sample that was not 1. Returns None if the whole window is stratum 1, which means
    "longer than this window", and the caller should say so rather than claim a date.
    """
    end = time.time()
    start = end - 30 * 86400
    series = vm.query_range(
        "max_over_time(chrony_tracking_stratum[300s])", start, end, 300
    )
    if not series:
        return None
    points = series[0][1]
    last_bad = None
    for ts, value in points:
        if value != 1:
            last_bad = ts
    if last_bad is None:
        return None
    return last_bad


def allan_deviation() -> list[tuple[float, float]]:
    """Overlapping Allan deviation of the oscillator's frequency corrections.

    chronyd's frequency correction is the rate it has to apply to keep the clock on the
    reference, so its stability over increasing averaging times is a measurement of the
    oscillator -- the standard way to show it is ADEV on log-log axes.

    The input is `chrony_tracking_frequency_ppms` in parts per million, converted to
    fractional frequency. The estimator is the overlapping Allan variance for frequency
    data:

        sigma_y^2(tau) = 1 / (2 m^2 (N - 2m + 1))
                         * sum_j ( sum_{i=j}^{j+m-1} (y_{i+m} - y_i) )^2

    with tau = m * tau0. Octave-spaced m, and a point is only emitted while at least ten
    independent estimates remain, below which the estimate is noise about noise.
    """
    end = time.time()
    start = end - ADEV_SECONDS
    series = vm.query_range(
        f"avg_over_time(chrony_tracking_frequency_ppms[{ADEV_TAU0}s])",
        start,
        end,
        ADEV_TAU0,
    )
    if not series:
        return []
    y = [value * 1e-6 for _, value in series[0][1]]
    return adev_from_frequency(y, ADEV_TAU0)


def adev_from_frequency(y: list[float], tau0: float) -> list[tuple[float, float]]:
    """The estimator above, split out so it can be tested against a known signal.

    Computed from a prefix sum rather than from the nested sums it is written with. The
    inner sum telescopes:

        sum_{i=j}^{j+m-1} (y_{i+m} - y_i) = S[j+2m] - 2*S[j+m] + S[j]

    so each averaging factor costs one pass instead of m passes. Written literally, the
    largest m alone is ~n^2/2 operations -- at the 21,600 samples a 30-day window holds
    that is hundreds of millions of steps in Python, per page render.
    """
    n = len(y)
    prefix = [0.0] * (n + 1)
    for i, value in enumerate(y):
        prefix[i + 1] = prefix[i] + value
    out: list[tuple[float, float]] = []
    m = 1
    while 2 * m + 10 <= n:
        total = 0.0
        count = n - 2 * m + 1
        for j in range(count):
            inner = prefix[j + 2 * m] - 2.0 * prefix[j + m] + prefix[j]
            total += inner * inner
        variance = total / (2 * m * m * count)
        if variance > 0:
            out.append((m * tau0, math.sqrt(variance)))
        m *= 2
    return out


# Summary statistics per window, as one query rather than twenty.
#
# `label_set` renames each result so the union keeps every term: `or` matches on the
# full label set, and because these differ in `stat` and `win` nothing is swallowed. A
# bare `avg_over_time(x[1h]) or stddev_over_time(x[1h])` would return only the first,
# since set operators drop `__name__` before comparing.
STAT_WINDOWS = ("1h", "24h", "7d", "30d")
_OFFSET = "chrony_tracking_last_offset_seconds"


def _stats_query() -> str:
    terms = []
    for win in STAT_WINDOWS:
        terms += [
            f'label_set(avg_over_time({_OFFSET}[{win}]), "stat", "mean", "win", "{win}")',
            f'label_set(stddev_over_time({_OFFSET}[{win}]), "stat", "sd", "win", "{win}")',
            # The peak excursion either side of zero. NOT
            # `max_over_time(abs(x)[30d])`: wrapping the selector in abs() makes it a
            # subquery, and a subquery needs an explicit step that at 30 days would
            # exceed the point budget. min and max over the raw selector cost nothing
            # and the larger magnitude is picked in Python.
            f'label_set(max_over_time({_OFFSET}[{win}]), "stat", "max", "win", "{win}")',
            f'label_set(min_over_time({_OFFSET}[{win}]), "stat", "min", "win", "{win}")',
            f'label_set(count_over_time({_OFFSET}[{win}]), "stat", "samples", "win", "{win}")',
            f'label_set(avg_over_time((chrony_tracking_stratum == bool 1)[{win}:300]),'
            f' "stat", "avail", "win", "{win}")',
        ]
    return " or ".join(terms)


def window_stats() -> list[dict[str, object]]:
    """Mean, standard deviation, peak excursion, availability and sample count.

    One instant query covers all four windows. Availability is sampled every 300 s, so
    an outage shorter than that can fall between two evaluations -- the same caveat as
    the lock calendar, and for the same reason.
    """
    grouped: dict[tuple[str, str], float] = {}
    for sample in vm.query(_stats_query()):
        win = sample.labels.get("win")
        stat = sample.labels.get("stat")
        if win and stat:
            grouped[(win, stat)] = sample.value

    rows = []
    for win in STAT_WINDOWS:
        hi = grouped.get((win, "max"))
        lo = grouped.get((win, "min"))
        peak = None
        if hi is not None or lo is not None:
            peak = max(
                (abs(v) for v in (hi, lo) if v is not None),
                default=None,
            )
        rows.append(
            {
                "window": win,
                "mean": grouped.get((win, "mean")),
                "sd": grouped.get((win, "sd")),
                "peak": peak,
                "samples": grouped.get((win, "samples")),
                "availability": grouped.get((win, "avail")),
            }
        )
    return rows
