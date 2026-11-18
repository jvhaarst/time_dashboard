"""time.vanhaarst.net -- the public status page of a stratum-1 NTP server.

Everything on the page is rendered server-side from one VictoriaMetrics query, so the
visitor's browser never talks to the metrics store and the page works with JavaScript
switched off.
"""

from __future__ import annotations

import logging
import os
import time

from flask import Flask, Response, render_template

from . import vm

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# Shown in the page's "about this server" block. Values rather than hard-coded strings
# so the chart can describe a different machine without a new image.
SERVER_NAME = os.environ.get("SERVER_NAME", "ntp.vanhaarst.net")
SERVER_LOCATION = os.environ.get("SERVER_LOCATION", "Wageningen, the Netherlands")
SERVER_HARDWARE = os.environ.get(
    "SERVER_HARDWARE", "Raspberry Pi with a Uputronics GPS/RTC HAT (u-blox MAX-M8Q)"
)
POOL_URL = os.environ.get("POOL_URL", "")

# How long a rendered page may be reused. The exporters scrape every 30 s, so anything
# shorter only adds load without adding information.
CACHE_SECONDS = float(os.environ.get("CACHE_SECONDS", "15"))

_cache: dict[str, object] = {"at": 0.0, "html": None}


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


def _offset_human(seconds: float | None) -> str:
    """A clock offset at the magnitude it actually has.

    A stratum-1 server's offsets are nanoseconds to microseconds, so a fixed unit is
    either unreadable or lies by rounding. The sign is kept: it says which way the
    clock is out.
    """
    if seconds is None:
        return "unknown"
    magnitude = abs(seconds)
    if magnitude < 1e-6:
        return f"{seconds * 1e9:+.0f} ns"
    if magnitude < 1e-3:
        return f"{seconds * 1e6:+.1f} µs"
    if magnitude < 1:
        return f"{seconds * 1e3:+.2f} ms"
    return f"{seconds:+.3f} s"


def _build_page() -> dict[str, object]:
    samples = vm.instant()

    stratum = vm.one(samples, "chrony_tracking_stratum")
    tracking = vm.labels_of(samples, "chrony_tracking_info")
    boot = vm.one(samples, "node_boot_time_seconds")
    clients = vm.by_label(samples, "ntp_clients_active", "window")
    received = vm.one(samples, "chrony_serverstats_ntp_packets_received_total")
    dropped = vm.one(samples, "chrony_serverstats_ntp_packets_dropped_total")
    mode = vm.one(samples, "gpsd_mode")

    # chrony_up is the exporter's "did chronyd answer" flag. Without it, every number
    # below is the last one scraped and could be minutes old, so the page says so
    # rather than presenting a stale value as current.
    up = vm.one(samples, "chrony_up")

    return {
        "healthy": bool(up) and stratum == 1,
        "chrony_up": bool(up),
        "stratum": stratum,
        "reference": tracking.get("tracking_name") or "unknown",
        "reference_refid": tracking.get("tracking_refid") or "",
        "offset": _offset_human(vm.one(samples, "chrony_tracking_last_offset_seconds")),
        "rms_offset": _offset_human(
            vm.one(samples, "chrony_tracking_rms_offset_seconds")
        ),
        "root_delay": _offset_human(
            vm.one(samples, "chrony_tracking_root_delay_seconds")
        ),
        "root_dispersion": _offset_human(
            vm.one(samples, "chrony_tracking_root_dispersion_seconds")
        ),
        "system_time": _offset_human(
            vm.one(samples, "chrony_tracking_system_time_seconds")
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
        "constellations": vm.satellites(samples),
        "clients": [
            (window, clients[window])
            for window in ("5m", "15m", "1h", "6h", "24h")
            if window in clients
        ],
        # The client log is a ring buffer sized by chronyd's `clientloglimit`: it counts
        # every address ever answered until eviction, so it measures the log, not demand.
        # It is here as a saturation indicator only, and the template labels it that way.
        "client_log_entries": vm.one(samples, "ntp_clients_log_entries"),
        "client_countries": vm.one(samples, "ntp_clients_countries_total"),
        "packets_received": received,
        "packets_dropped": dropped,
        "packets_per_second": vm.packets_per_second(),
        "uptime": _seconds_human(time.time() - boot) if boot else "unknown",
        "server_name": SERVER_NAME,
        "server_location": SERVER_LOCATION,
        "server_hardware": SERVER_HARDWARE,
        "pool_url": POOL_URL,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }


@app.route("/")
def index() -> Response:
    now = time.monotonic()
    if _cache["html"] is not None and now - float(_cache["at"]) < CACHE_SECONDS:
        return Response(_cache["html"], mimetype="text/html")
    try:
        page = _build_page()
    except vm.VictoriaMetricsError as exc:
        log.warning("metrics unavailable: %s", exc)
        # 503, not 200 with an empty page: a monitor should be able to tell.
        return Response(
            render_template("unavailable.html", reason=str(exc)),
            status=503,
            mimetype="text/html",
        )
    html = render_template("index.html", **page)
    _cache["html"] = html
    _cache["at"] = now
    return Response(html, mimetype="text/html")


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
        return Response(f"metrics unavailable: {exc}\n", status=503,
                        mimetype="text/plain")
    return Response("ok\n", mimetype="text/plain")
