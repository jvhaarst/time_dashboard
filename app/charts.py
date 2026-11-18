"""Inline SVG charts, drawn server-side.

No charting library and no JavaScript: the page is rendered on the server, so the charts
are too. That keeps the page one request, keeps it working with JavaScript off, and
avoids pulling a library from a CDN into a page whose whole point is to be dependable.

Every chart here paints with CSS custom properties rather than literal colours, so the
same SVG follows the page's light and dark theme.
"""

from __future__ import annotations

import html
import math
import time
from typing import Iterable, Sequence

Point = tuple[float, float]

# A line per network source. Enough hues to tell eight apart, chosen to stay legible on
# both backgrounds rather than to look like a palette.
SERIES_COLOURS = (
    "#0b5f8a",
    "#b5651d",
    "#2e7d32",
    "#8e24aa",
    "#c62828",
    "#00838f",
    "#5d4037",
    "#6a7f00",
)


#: Most points a single polyline is drawn with. A 7-day window at a 600 s step is 1,008
#: points per series and the peers chart has eight of them; at ~14 characters per
#: coordinate pair that alone is over 100 KB of markup for detail no screen can show,
#: since the chart is under 800 px wide. Decimating to this keeps every visible feature.
MAX_POINTS = 400


def _decimate(points: Sequence[Point]) -> list[Point]:
    """Evenly thin a series, always keeping the first and last point.

    Thinning, not averaging: the mean line is already a rollup, and the band's extremes
    must survive -- averaging them here would undo the reason the band exists.
    """
    n = len(points)
    if n <= MAX_POINTS:
        return list(points)
    stride = n / MAX_POINTS
    out = [points[min(int(i * stride), n - 1)] for i in range(MAX_POINTS)]
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _nice_offset(seconds: float) -> str:
    magnitude = abs(seconds)
    if magnitude < 1e-6:
        return f"{seconds * 1e9:.0f} ns"
    if magnitude < 1e-3:
        return f"{seconds * 1e6:.1f} µs"
    if magnitude < 1:
        return f"{seconds * 1e3:.2f} ms"
    return f"{seconds:.3f} s"


def _time_ticks(start: float, end: float, count: int = 5) -> list[tuple[float, str]]:
    span = end - start
    fmt = "%H:%M" if span <= 3 * 86400 else "%d %b"
    return [
        (
            start + span * i / (count - 1),
            time.strftime(fmt, time.gmtime(start + span * i / (count - 1))),
        )
        for i in range(count)
    ]


def _symmetric_bounds(values: Iterable[float]) -> tuple[float, float]:
    """Y bounds that keep zero visible and never collapse to a single line.

    A clock offset is meaningful relative to zero, so a chart that auto-scales to the
    data alone turns a flat 50 ns trace into a dramatic mountain range. Zero stays in
    view and the range is padded by a tenth.
    """
    seen = [v for v in values if v is not None and math.isfinite(v)]
    if not seen:
        return (-1e-9, 1e-9)
    lo, hi = min(seen + [0.0]), max(seen + [0.0])
    if hi == lo:
        hi = lo + 1e-9
    pad = (hi - lo) * 0.1
    return (lo - pad, hi + pad)


def offset_chart(
    mean: Sequence[Point],
    low: Sequence[Point],
    high: Sequence[Point],
    start: float,
    end: float,
    width: int = 760,
    height: int = 240,
) -> str:
    """Mean line with a min-max band, which is the shape of the question.

    The band is what makes a rolled-up series honest: at a 30-minute step the mean hides
    every excursion, and an excursion is the only thing worth seeing on a clock that is
    otherwise flat for a month.
    """
    pad_l, pad_r, pad_t, pad_b = 64, 10, 12, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    if not mean:
        return _empty(width, height, "no data in this window")

    y_lo, y_hi = _symmetric_bounds(
        [v for _, v in mean] + [v for _, v in low] + [v for _, v in high]
    )

    def sx(ts: float) -> float:
        return pad_l + (ts - start) / max(end - start, 1) * plot_w

    def sy(value: float) -> float:
        return pad_t + (y_hi - value) / (y_hi - y_lo) * plot_h

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
    ]

    # Horizontal grid, with zero drawn solid so the eye has a reference.
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        value = y_hi - frac * (y_hi - y_lo)
        y = sy(value)
        parts.append(
            f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="axis" x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end">'
            f"{_esc(_nice_offset(value))}</text>"
        )
    if y_lo < 0 < y_hi:
        zero = sy(0.0)
        parts.append(
            f'<line class="zero" x1="{pad_l}" y1="{zero:.1f}" x2="{width - pad_r}" y2="{zero:.1f}"/>'
        )

    for ts, label in _time_ticks(start, end):
        x = sx(ts)
        parts.append(
            f'<text class="axis" x="{x:.1f}" y="{height - 8}" text-anchor="middle">'
            f"{_esc(label)}</text>"
        )

    if low and high:
        top = " ".join(f"{sx(ts):.1f},{sy(v):.1f}" for ts, v in _decimate(high))
        bottom = " ".join(
            f"{sx(ts):.1f},{sy(v):.1f}" for ts, v in reversed(_decimate(low))
        )
        parts.append(f'<polygon class="band" points="{top} {bottom}"/>')

    line = " ".join(f"{sx(ts):.1f},{sy(v):.1f}" for ts, v in _decimate(mean))
    parts.append(f'<polyline class="line" points="{line}"/>')
    parts.append("</svg>")
    return "".join(parts)


def peers_chart(
    series: Sequence[dict],
    start: float,
    end: float,
    width: int = 760,
    height: int = 240,
) -> str:
    """One line per network source: this clock measured against each of them."""
    pad_l, pad_r, pad_t, pad_b = 64, 10, 12, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    all_values = [v for s in series for _, v in s["points"]]
    if not all_values:
        return _empty(width, height, "no data in this window")
    y_lo, y_hi = _symmetric_bounds(all_values)

    def sx(ts: float) -> float:
        return pad_l + (ts - start) / max(end - start, 1) * plot_w

    def sy(value: float) -> float:
        return pad_t + (y_hi - value) / (y_hi - y_lo) * plot_h

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
    ]
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        value = y_hi - frac * (y_hi - y_lo)
        y = sy(value)
        parts.append(
            f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="axis" x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end">'
            f"{_esc(_nice_offset(value))}</text>"
        )
    if y_lo < 0 < y_hi:
        zero = sy(0.0)
        parts.append(
            f'<line class="zero" x1="{pad_l}" y1="{zero:.1f}" x2="{width - pad_r}" y2="{zero:.1f}"/>'
        )
    for ts, label in _time_ticks(start, end):
        parts.append(
            f'<text class="axis" x="{sx(ts):.1f}" y="{height - 8}" text-anchor="middle">'
            f"{_esc(label)}</text>"
        )
    for i, s in enumerate(series):
        colour = SERIES_COLOURS[i % len(SERIES_COLOURS)]
        line = " ".join(
            f"{sx(ts):.1f},{sy(v):.1f}" for ts, v in _decimate(s["points"])
        )
        parts.append(
            f'<polyline class="peer" stroke="{colour}" points="{line}">'
            f"<title>{_esc(s['name'])}</title></polyline>"
        )
    parts.append("</svg>")
    return "".join(parts)


def peers_legend(series: Sequence[dict]) -> str:
    items = []
    for i, s in enumerate(series):
        colour = SERIES_COLOURS[i % len(SERIES_COLOURS)]
        items.append(
            f'<li><span class="swatch" style="background:{colour}"></span>'
            f"{_esc(s['name'])}</li>"
        )
    return f'<ul class="legend">{"".join(items)}</ul>'


def calendar(days: Sequence[dict], width: int = 760) -> str:
    """One cell per day, coloured by how much of the day the clock stayed locked.

    Buckets, not a gradient: the only distinctions that matter operationally are "fine",
    "it wobbled" and "it broke". A day with no samples is drawn empty, because a gap in
    monitoring is not an outage and colouring it red would claim an incident that may
    never have happened.
    """
    if not days:
        return _empty(width, 90, "no data")
    cell = 22
    gap = 3
    cols = len(days)
    svg_w = cols * (cell + gap)
    height = cell + 30
    parts = [
        f'<svg class="calendar" viewBox="0 0 {svg_w} {height}" role="img" '
        f'xmlns="http://www.w3.org/2000/svg">'
    ]
    for i, day in enumerate(days):
        avail = day.get("availability")
        if avail is None:
            klass = "cal-none"
            label = "no data"
        elif avail >= 0.999:
            klass = "cal-good"
            label = f"{avail * 100:.2f}% locked"
        elif avail >= 0.99:
            klass = "cal-ok"
            label = f"{avail * 100:.2f}% locked"
        else:
            klass = "cal-bad"
            label = f"{avail * 100:.2f}% locked"
        x = i * (cell + gap)
        date = time.strftime("%Y-%m-%d", time.gmtime(day["timestamp"]))
        parts.append(
            f'<rect class="{klass}" x="{x}" y="0" width="{cell}" height="{cell}" rx="3">'
            f"<title>{_esc(date)}: {_esc(label)}</title></rect>"
        )
        if i == 0 or i == cols - 1 or time.gmtime(day["timestamp"]).tm_mday == 1:
            parts.append(
                f'<text class="axis" x="{x + cell / 2:.0f}" y="{cell + 14}" '
                f'text-anchor="middle">{_esc(time.strftime("%d %b", time.gmtime(day["timestamp"])))}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def adev_chart(
    points: Sequence[Point], width: int = 760, height: int = 240
) -> str:
    """Allan deviation on log-log axes, which is the only way it reads.

    Both axes span decades: tau from a couple of minutes to days, sigma over several
    orders of magnitude. On linear axes every point but the first collapses onto the
    axis.
    """
    usable = [(t, s) for t, s in points if t > 0 and s > 0]
    if len(usable) < 2:
        return _empty(width, height, "not enough history yet")
    pad_l, pad_r, pad_t, pad_b = 64, 10, 12, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    lx = [math.log10(t) for t, _ in usable]
    ly = [math.log10(s) for _, s in usable]
    x_lo, x_hi = math.floor(min(lx)), math.ceil(max(lx))
    y_lo, y_hi = math.floor(min(ly)), math.ceil(max(ly))
    if x_hi == x_lo:
        x_hi += 1
    if y_hi == y_lo:
        y_hi += 1

    def sx(value: float) -> float:
        return pad_l + (value - x_lo) / (x_hi - x_lo) * plot_w

    def sy(value: float) -> float:
        return pad_t + (y_hi - value) / (y_hi - y_lo) * plot_h

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
    ]
    for decade in range(int(y_lo), int(y_hi) + 1):
        y = sy(decade)
        parts.append(
            f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="axis" x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end">'
            f"1e{decade}</text>"
        )
    for decade in range(int(x_lo), int(x_hi) + 1):
        x = sx(decade)
        seconds = 10.0**decade
        if seconds >= 86400:
            label = f"{seconds / 86400:.0f}d"
        elif seconds >= 3600:
            label = f"{seconds / 3600:.0f}h"
        elif seconds >= 60:
            label = f"{seconds / 60:.0f}m"
        else:
            label = f"{seconds:.0f}s"
        parts.append(
            f'<line class="grid" x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t + plot_h}"/>'
        )
        parts.append(
            f'<text class="axis" x="{x:.1f}" y="{height - 8}" text-anchor="middle">'
            f"{_esc(label)}</text>"
        )
    line = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(lx, ly))
    parts.append(f'<polyline class="line" points="{line}"/>')
    for x, y in zip(lx, ly):
        parts.append(f'<circle class="dot" cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.5"/>')
    parts.append("</svg>")
    return "".join(parts)


def satellite_chart(
    satellites: Sequence[dict], width: int = 760, height: int = 200
) -> str:
    """A bar per satellite, signal strength as height, grouped by constellation.

    Bars rather than a sky plot: azimuth and elevation are in the store, but what a
    reader can act on is "are there enough strong signals", and a bar chart answers that
    at a glance on a phone.
    """
    if not satellites:
        return _empty(width, height, "no satellites reported")
    pad_l, pad_r, pad_t, pad_b = 36, 10, 12, 34
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    top = max(50.0, max(s["ss"] for s in satellites))
    slot = plot_w / len(satellites)
    bar = max(3.0, slot * 0.7)

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'xmlns="http://www.w3.org/2000/svg">'
    ]
    for value in (0, 20, 30, 40, 50):
        if value > top:
            continue
        y = pad_t + (1 - value / top) * plot_h
        parts.append(
            f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="axis" x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end">{value}</text>'
        )
    for i, sat in enumerate(satellites):
        x = pad_l + i * slot + (slot - bar) / 2
        h = max(1.0, sat["ss"] / top * plot_h)
        y = pad_t + plot_h - h
        klass = "sat-used" if sat["used"] else "sat-seen"
        parts.append(
            f'<rect class="{klass}" x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" '
            f'height="{h:.1f}" rx="1">'
            f"<title>{_esc(sat['constellation'])} {_esc(sat['prn'])}: "
            f"{sat['ss']:.0f} dB-Hz, {'used' if sat['used'] else 'not used'}</title></rect>"
        )
    # One label per constellation, under the middle of its run of bars.
    runs: list[tuple[str, int, int]] = []
    for i, sat in enumerate(satellites):
        if runs and runs[-1][0] == sat["constellation"]:
            name, first, _ = runs[-1]
            runs[-1] = (name, first, i)
        else:
            runs.append((sat["constellation"], i, i))
    for name, first, last in runs:
        centre = pad_l + (first + last + 1) / 2 * slot
        parts.append(
            f'<text class="axis" x="{centre:.1f}" y="{height - 10}" '
            f'text-anchor="middle">{_esc(name)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _empty(width: int, height: int, message: str) -> str:
    return (
        f'<svg class="chart empty" viewBox="0 0 {width} {height}" role="img" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<text class="axis" x="{width / 2:.0f}" y="{height / 2:.0f}" '
        f'text-anchor="middle">{_esc(message)}</text></svg>'
    )
