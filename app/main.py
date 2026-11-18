"""time.vanhaarst.net -- the public status page of a stratum-1 NTP server.

Everything is rendered server-side from VictoriaMetrics, charts included, so the page is
one request and works with JavaScript switched off. The only JavaScript is a ticking
clock and a poll of /api/status that refreshes the live figures in place.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time

from flask import Flask, Response, jsonify, render_template, request
from markupsafe import Markup, escape

from . import charts, history, vm

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

SERVER_NAME = os.environ.get("SERVER_NAME", "ntp.vanhaarst.net")
SERVER_LOCATION = os.environ.get("SERVER_LOCATION", "Doorwerth, the Netherlands")
SERVER_HARDWARE = os.environ.get(
    "SERVER_HARDWARE", "Raspberry Pi with a Uputronics GPS/RTC HAT (u-blox MAX-M8Q)"
)
POOL_URL = os.environ.get("POOL_URL", "")

# Whether to tell visitors the server answers over IPv6. Default false, and that is not
# pessimism: ntp.vanhaarst.net publishes an AAAA, chronyd listens on the IPv6 wildcard
# and `allow all` is set, but inbound IPv6 is refused one hop UPSTREAM of the router --
# 2001:4c3c:0:3d::1:17 answers every packet with "Destination unreachable:
# Administratively prohibited", for ICMP, TCP 22 and UDP 123 alike. Claiming IPv6 on the
# page would send IPv6-preferring clients down a path that cannot work.
SERVER_IPV6 = os.environ.get("SERVER_IPV6", "").strip().lower() in {"1", "true", "yes"}

# Page cache. Longer than the 10 s client poll on purpose: the poll refreshes the live
# figures from /api/status, which is one cheap instant query, while a full render also
# draws the history charts.
CACHE_SECONDS = float(os.environ.get("CACHE_SECONDS", "60"))
STATUS_CACHE_SECONDS = float(os.environ.get("STATUS_CACHE_SECONDS", "10"))

# The history pieces are cached for much longer than the page. A 30-day range query is
# ~21,000 points per series; nothing in it changes inside a minute, and the Allan
# deviation changes meaningfully only over days.
HISTORY_TTL = float(os.environ.get("HISTORY_TTL", "120"))
CALENDAR_TTL = float(os.environ.get("CALENDAR_TTL", "900"))
ADEV_TTL = float(os.environ.get("ADEV_TTL", "3600"))

_page_cache: dict[str, tuple[float, str]] = {}


def _seconds_human(seconds: float) -> str:
    """A duration as "3d 4h", "4h 12m" or "12m" -- never more than two units."""
    seconds = int(max(seconds, 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


SIGNIFICANT = 4


def _decimal_seconds(seconds: float, sign: bool = True) -> str:
    """A duration as plain decimal seconds, never scaled and never in exponent form.

    Scaled units read well on their own but badly in a column: `639 ns`, `18.6 us` and
    `-39.95 ms` are three different scales, so the eye cannot rank them without doing
    arithmetic, and a unit that changes between refreshes makes a steady figure look
    like it jumped. One unit for everything makes the magnitude the thing you see.

    The decimal count follows the value, so four significant figures survive at any
    magnitude: 0.000000639 keeps its 639, and 118.7 does not grow a tail of zeros.
    """
    if seconds == 0:
        return "+0" if sign else "0"
    magnitude = abs(seconds)
    exponent = math.floor(math.log10(magnitude))
    decimals = min(12, max(0, SIGNIFICANT - 1 - exponent))
    text = f"{seconds:+.{decimals}f}" if sign else f"{magnitude:.{decimals}f}"
    # Trailing zeros carry no information once the significant figures are in.
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _offset_human(seconds: float | None) -> str:
    """A clock offset in seconds, as plain text. The sign says which way it is out."""
    if seconds is None:
        return "unknown"
    return _decimal_seconds(seconds) + " s"


# A sign, "0.", then the run of zeros before the first significant digit.
_LEADING_ZEROS = re.compile(r"^(?P<head>[+-]?0\.)(?P<zeros>0+)(?P<rest>.*)$")


def _mark_zeros(text: str) -> Markup:
    """Shade the leading zeros in pairs, alternating between two greys.

    `0.0000007203` and `0.000007203` differ by one character in the middle of a run of
    zeros, which no amount of monospacing makes visible: the eye cannot count six
    identical glyphs against seven. Banding them two at a time turns counting into
    pattern matching -- three bands is a microsecond, four and a half is a nanosecond --
    and keeps the significant digits as the only full-contrast part of the number.

    Grey on grey rather than a colour: the shading is a reading aid, not a status, and
    this page already uses colour to mean "good" and "bad".
    """
    match = _LEADING_ZEROS.match(text)
    if not match:
        return Markup(escape(text))
    zeros = match["zeros"]
    bands = []
    for start in range(0, len(zeros), 2):
        band = "z0" if (start // 2) % 2 == 0 else "z1"
        bands.append(f'<span class="{band}">{zeros[start:start + 2]}</span>')
    # `str()` around each escape() is not decoration: Markup.__add__ ESCAPES a plain
    # str operand, so `escape(head) + "<span ...>"` would escape the spans and the page
    # would show the markup as text.
    return Markup(
        str(escape(match["head"])) + "".join(bands) + str(escape(match["rest"]))
    )


def _offset_cell(seconds: float | None) -> Markup:
    """The same figure inside a table, where a blank beats the word "unknown"."""
    if seconds is None:
        return Markup("")
    return _mark_zeros(_decimal_seconds(seconds)) + Markup(" s")


def _magnitude_cell(seconds: float | None) -> Markup:
    """A peak excursion, which is a size and so carries no sign."""
    if seconds is None:
        return Markup("")
    return _mark_zeros(_decimal_seconds(seconds, sign=False)) + Markup(" s")


app.jinja_env.globals["fmt_magnitude"] = _magnitude_cell


# Used by the sources table, which formats a dozen offsets per row. NOT named `offset`:
# the template already receives a context variable of that name (the current offset as
# text), and a context variable shadows a global, so the call would fail with
# "'str' object is not callable".
app.jinja_env.globals["fmt_offset"] = _offset_cell


def _live(samples: dict[str, list[vm.Sample]]) -> dict[str, object]:
    """The figures that change every scrape, shared by the page and /api/status."""
    stratum = vm.one(samples, "chrony_tracking_stratum")
    tracking = vm.labels_of(samples, "chrony_tracking_info")
    mode = vm.one(samples, "gpsd_mode")
    up = vm.one(samples, "chrony_up")
    boot = vm.one(samples, "node_boot_time_seconds")
    return {
        "healthy": bool(up) and stratum == 1,
        "chrony_up": bool(up),
        "stratum": stratum,
        "reference": tracking.get("tracking_name") or "unknown",
        "reference_refid": tracking.get("tracking_refid") or "",
        "offset_seconds": vm.one(samples, "chrony_tracking_last_offset_seconds"),
        "rms_seconds": vm.one(samples, "chrony_tracking_rms_offset_seconds"),
        "offset": _offset_human(vm.one(samples, "chrony_tracking_last_offset_seconds")),
        "rms_offset": _offset_human(
            vm.one(samples, "chrony_tracking_rms_offset_seconds")
        ),
        "root_delay_seconds": vm.one(samples, "chrony_tracking_root_delay_seconds"),
        "root_dispersion_seconds": vm.one(
            samples, "chrony_tracking_root_dispersion_seconds"
        ),
        "pps_seconds": vm.source_offset(samples, "PPS"),
        "gps_seconds": vm.source_offset(samples, "GPS"),
        "root_delay": _offset_human(
            vm.one(samples, "chrony_tracking_root_delay_seconds")
        ),
        "root_dispersion": _offset_human(
            vm.one(samples, "chrony_tracking_root_dispersion_seconds")
        ),
        "skew_ppm": vm.one(samples, "chrony_tracking_skew_ppms"),
        "frequency_ppm": vm.one(samples, "chrony_tracking_frequency_ppms"),
        "update_interval": vm.one(samples, "chrony_tracking_update_interval_seconds"),
        "pps_offset": _offset_human(vm.source_offset(samples, "PPS")),
        "gps_offset": _offset_human(vm.source_offset(samples, "GPS")),
        "fix_mode": vm.FIX_MODES.get(
            str(int(mode)) if mode is not None else "", "unknown"
        ),
        "sats_seen": vm.one(samples, "gpsd_nSat"),
        "sats_used": vm.one(samples, "gpsd_uSat"),
        "pdop": vm.one(samples, "gpsd_pdop"),
        "tdop": vm.one(samples, "gpsd_tdop"),
        "hdop": vm.one(samples, "gpsd_hdop"),
        "leap_seconds": vm.one(samples, "gpsd_leapseconds"),
        "mean_signal": vm.mean_signal(samples),
        "temperature": vm.one(samples, "node_hwmon_temp_celsius"),
        "uptime": _seconds_human(time.time() - boot) if boot else "unknown",
        "packets_received": vm.one(
            samples, "chrony_serverstats_ntp_packets_received_total"
        ),
        "packets_dropped": vm.one(
            samples, "chrony_serverstats_ntp_packets_dropped_total"
        ),
        "client_log_entries": vm.one(samples, "ntp_clients_log_entries"),
        "client_countries": vm.one(samples, "ntp_clients_countries_total"),
    }


def _build_page(range_key: str) -> str:
    samples = vm.instant()
    live = _live(samples)
    clients = vm.by_label(samples, "ntp_clients_active", "window")

    offsets = history.cached(
        f"offset:{range_key}", HISTORY_TTL, lambda: history.offset_history(range_key)
    )
    peers = history.cached(
        f"peers:{range_key}", HISTORY_TTL, lambda: history.peer_history(range_key)
    )
    days = history.cached("calendar", CALENDAR_TTL, history.lock_calendar)
    stats = history.cached("stats", HISTORY_TTL, history.window_stats)
    adev = history.cached("adev", ADEV_TTL, history.allan_deviation)
    since = history.cached("locked_since", CALENDAR_TTL, history.locked_since)

    sats = vm.satellite_detail(samples)

    tile = next((r for r in stats if r.get("window") == "tile"), {})
    locked_days = [d for d in days if d.get("availability") is not None]
    availability = (
        sum(d["availability"] for d in locked_days) / len(locked_days)
        if locked_days
        else None
    )

    return render_template(
        "index.html",
        ranges=list(history.RANGES),
        range_key=range_key,
        offset_chart=charts.offset_chart(
            offsets["mean"],
            offsets["min"],
            offsets["max"],
            offsets["start"],
            offsets["end"],
        ),
        peers_chart=charts.peers_chart(
            peers, offsets["start"], offsets["end"]
        ),
        peers_legend=charts.peers_legend(peers),
        calendar=charts.calendar(days),
        adev_chart=charts.adev_chart(adev),
        satellite_chart=charts.satellite_chart(sats),
        sources=history.sources(),
        stats=[r for r in stats if r.get("window") != "tile"],
        sys_sd=tile.get("sys_sd"),
        pps_sd=tile.get("pps_sd"),
        constellations=vm.satellites(samples),
        availability=availability,
        calendar_days=len(days),
        locked_since=(
            time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(since)) if since else None
        ),
        locked_for=_seconds_human(time.time() - since) if since else None,
        clients=[
            (window, clients[window])
            for window in ("5m", "15m", "1h", "6h", "24h")
            if window in clients
        ],
        packets_per_second=vm.packets_per_second(),
        server_name=SERVER_NAME,
        server_location=SERVER_LOCATION,
        server_hardware=SERVER_HARDWARE,
        pool_url=POOL_URL,
        ipv6=SERVER_IPV6,
        generated=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        # Rendered into the clock so a visitor without JavaScript sees a time rather
        # than a row of dashes. It is the render time, so it can be a minute stale --
        # the page says as much next to it.
        clock_initial=time.strftime("%H:%M:%S", time.gmtime()),
        generated_date=time.strftime("%A %d %B %Y", time.gmtime()),
        **live,
    )


@app.route("/")
def index() -> Response:
    range_key = request.args.get("range", history.DEFAULT_RANGE)
    if range_key not in history.RANGES:
        range_key = history.DEFAULT_RANGE
    now = time.monotonic()
    hit = _page_cache.get(range_key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return Response(hit[1], mimetype="text/html")
    try:
        html = _build_page(range_key)
    except vm.VictoriaMetricsError as exc:
        log.warning("metrics unavailable: %s", exc)
        # 503, not 200 with an empty page: a monitor should be able to tell.
        return Response(
            render_template("unavailable.html", reason=str(exc)),
            status=503,
            mimetype="text/html",
        )
    _page_cache[range_key] = (now, html)
    return Response(html, mimetype="text/html")


@app.route("/api/time")
def api_time() -> Response:
    """The server's clock, for the browser to measure itself against.

    Deliberately the cheapest route on the site: no VictoriaMetrics query, no cache, no
    template. It is called several times per visit and its latency IS the measurement
    error, so anything done here makes the answer worse.

    One timestamp is returned, not a receive/transmit pair. In a synchronous worker the
    two would differ by microseconds while the network path contributes milliseconds,
    so the extra field would imply a precision that is not there.
    """
    payload = jsonify({"t": time.time()})
    # Any cache between here and the browser would return a stale timestamp and the
    # visitor would be told their clock is wrong by exactly the cache age.
    payload.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    payload.headers["Pragma"] = "no-cache"
    return payload


@app.route("/api/status")
def api_status() -> Response:
    """The live figures as JSON, for the page's own poll and for anyone else."""

    def build() -> dict[str, object]:
        samples = vm.instant()
        payload = dict(_live(samples))
        payload["now"] = time.time()
        payload["clients"] = vm.by_label(samples, "ntp_clients_active", "window")
        payload["satellites"] = vm.satellites(samples)
        payload["sources"] = history.sources()
        return payload

    try:
        payload = history.cached("api:status", STATUS_CACHE_SECONDS, build)
    except vm.VictoriaMetricsError as exc:
        return jsonify({"status": "error", "error": str(exc)}), 503
    response = jsonify(payload)
    response.headers["Cache-Control"] = f"public, max-age={int(STATUS_CACHE_SECONDS)}"
    return response


@app.route("/api/history")
def api_history() -> Response:
    """Offset history for one window, as JSON."""
    range_key = request.args.get("range", history.DEFAULT_RANGE)
    if range_key not in history.RANGES:
        return jsonify(
            {"status": "error", "error": f"range must be one of {list(history.RANGES)}"}
        ), 400
    try:
        payload = history.cached(
            f"offset:{range_key}", HISTORY_TTL, lambda: history.offset_history(range_key)
        )
    except vm.VictoriaMetricsError as exc:
        return jsonify({"status": "error", "error": str(exc)}), 503
    response = jsonify(payload)
    response.headers["Cache-Control"] = f"public, max-age={int(HISTORY_TTL)}"
    return response


@app.route("/healthz")
def healthz() -> Response:
    """Liveness only: is this process serving?

    It deliberately does NOT query VictoriaMetrics. A liveness probe that depends on
    another service restarts a perfectly good pod whenever that service is slow, which
    is how the zeildashboard probe took the site down for nine minutes.
    """
    return Response("ok\n", mimetype="text/plain")


@app.route("/readyz")
def readyz() -> Response:
    """Readiness: can this process reach the metrics store?"""
    try:
        vm.one(vm.instant(), "chrony_up")
    except vm.VictoriaMetricsError as exc:
        return Response(
            f"metrics unavailable: {exc}\n", status=503, mimetype="text/plain"
        )
    return Response("ok\n", mimetype="text/plain")
