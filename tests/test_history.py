"""Tests for the range-query layer: escaping, Allan deviation, calendar shaping.

Each of these covers a failure that produced a plausible-looking page or a silently
wrong number rather than an exception.
"""

import math
import random

import pytest

from app import history


def test_re2_quote_escapes_dots_but_not_hyphens():
    """RE2 rejects `\\-`; `re.escape` produces it, which is why there is a custom one."""
    assert history.re2_quote("ntp1.time.nl") == r"ntp1\.time\.nl"
    assert history.re2_quote("static.ip-031-223-173-226.signet.nl") == (
        r"static\.ip-031-223-173-226\.signet\.nl"
    )
    assert "\\-" not in history.re2_quote("a-b-c")


def test_re2_quote_escapes_every_re2_metacharacter():
    for ch in ".+*?()[]{}|^$\\":
        assert history.re2_quote(ch) == "\\" + ch


def test_label_regex_doubles_backslashes_for_the_string_literal():
    """MetricsQL parses the string literal first, and rejects `\\.` inside one."""
    out = history.label_regex(["a.b", "c.d"])
    assert out == '"a\\\\.b|c\\\\.d"'
    assert out.startswith('"') and out.endswith('"')


def test_label_regex_escapes_embedded_quotes():
    assert '\\"' in history.label_regex(['we"ird'])


def test_adev_of_a_constant_frequency_is_zero_so_nothing_is_emitted():
    """A perfectly stable oscillator has no Allan deviation to plot."""
    assert history.adev_from_frequency([2.5] * 64, 1.0) == []


def test_adev_of_an_alternating_signal_matches_the_closed_form():
    """y = [a, -a, a, -a, ...] gives sigma(tau0) = a * sqrt(2), exactly."""
    a = 3.0
    y = [a if i % 2 == 0 else -a for i in range(128)]
    points = history.adev_from_frequency(y, tau0=1.0)
    tau, sigma = points[0]
    assert tau == 1.0
    assert sigma == pytest.approx(a * math.sqrt(2), rel=1e-12)


def test_adev_prefix_sum_matches_the_literal_estimator():
    """The optimisation must not change the answer, only the cost."""

    def naive(y, tau0):
        n = len(y)
        out = []
        m = 1
        while 2 * m + 10 <= n:
            total = 0.0
            count = n - 2 * m + 1
            for j in range(count):
                inner = sum(y[i + m] - y[i] for i in range(j, j + m))
                total += inner * inner
            variance = total / (2 * m * m * count)
            if variance > 0:
                out.append((m * tau0, math.sqrt(variance)))
            m *= 2
        return out

    rng = random.Random(20261118)
    y = [rng.gauss(0, 1e-7) + i * 1e-10 for i in range(200)]
    fast = history.adev_from_frequency(y, 60.0)
    slow = naive(y, 60.0)
    assert len(fast) == len(slow)
    for (t1, s1), (t2, s2) in zip(fast, slow):
        assert t1 == t2
        assert s1 == pytest.approx(s2, rel=1e-9)


def test_adev_tau_values_are_octave_spaced():
    y = [float(i % 7) for i in range(300)]
    taus = [t for t, _ in history.adev_from_frequency(y, 120.0)]
    assert taus == [120.0, 240.0, 480.0, 960.0, 1920.0, 3840.0, 7680.0, 15360.0]


def test_adev_stops_while_estimates_remain_meaningful():
    """Never emits a point computed from fewer than ten differences."""
    y = [float(i) for i in range(50)]
    for tau, _ in history.adev_from_frequency(y, 1.0):
        m = int(tau)
        assert 2 * m + 10 <= len(y)


def test_lock_calendar_reports_none_for_a_day_with_no_samples(monkeypatch):
    """A gap in monitoring is not an outage and must not be drawn as one."""

    def fake_range(expr, start, end, step):
        if expr.startswith("avg_over_time"):
            return [({}, [(1000.0, 1.0), (2000.0, 0.5)])]
        return [({}, [(1000.0, 2880.0), (2000.0, 2880.0), (3000.0, 0.0)])]

    monkeypatch.setattr(history.vm, "query_range", fake_range)
    days = history.lock_calendar(3)
    assert [d["availability"] for d in days] == [1.0, 0.5, None]
    assert [d["samples"] for d in days] == [2880, 2880, 0]


def test_lock_calendar_uses_an_explicit_subquery_step(monkeypatch):
    """Without `:300` the inner expression is evaluated once per day and the whole
    calendar reads 0.036% on a server that never lost lock."""
    seen = []

    def fake_range(expr, start, end, step):
        seen.append(expr)
        return [({}, [])]

    monkeypatch.setattr(history.vm, "query_range", fake_range)
    history.lock_calendar(3)
    assert any("[1d:300]" in expr for expr in seen)


def test_cached_rebuilds_after_the_ttl(monkeypatch):
    calls = []

    def build():
        calls.append(1)
        return len(calls)

    history._cache.clear()
    assert history.cached("k", 1000, build) == 1
    assert history.cached("k", 1000, build) == 1
    assert history.cached("k", 0, build) == 2
