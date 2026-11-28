"""Unit tests for the parts that turn VictoriaMetrics output into page figures.

These are the places where a wrong assumption produces a plausible-looking page rather
than an error, which is why they are the parts under test.
"""

from app import vm
from app.main import _offset_human, _seconds_human, _si_seconds


def s(value, **labels):
    return vm.Sample(value=value, labels=labels)


def test_satellites_counts_used_from_the_label_not_from_gpsd_sat_used():
    """`used` is a label on gpsd_sat_ss, written as Python's str(bool)."""
    samples = {
        "gpsd_sat_ss": [
            s(42.0, gnssid="0", PRN="1", used="True"),
            s(38.0, gnssid="0", PRN="2", used="True"),
            s(21.0, gnssid="0", PRN="3", used="False"),
            s(30.0, gnssid="2", PRN="9", used="True"),
            s(12.0, gnssid="1", PRN="33", used="False"),
        ]
    }
    rows = {r["name"]: r for r in vm.satellites(samples)}
    assert rows["GPS"] == {"gnssid": "0", "name": "GPS", "seen": 3, "used": 2}
    assert rows["Galileo"]["used"] == 1
    assert rows["SBAS"]["used"] == 0
    assert rows["SBAS"]["seen"] == 1


def test_satellites_accepts_other_truthy_spellings():
    samples = {"gpsd_sat_ss": [s(1.0, gnssid="0", used="true"),
                               s(1.0, gnssid="0", used="1")]}
    assert vm.satellites(samples)[0]["used"] == 2


def test_satellites_sorts_most_used_first():
    samples = {
        "gpsd_sat_ss": [
            s(1.0, gnssid="1", PRN="a", used="False"),
            s(1.0, gnssid="0", PRN="b", used="True"),
            s(1.0, gnssid="0", PRN="c", used="True"),
            s(1.0, gnssid="2", PRN="d", used="True"),
        ]
    }
    assert [r["name"] for r in vm.satellites(samples)] == ["GPS", "Galileo", "SBAS"]


def test_by_label_keys_on_the_requested_label():
    samples = {"ntp_clients_active": [s(188.0, window="5m"), s(1442.0, window="1h")]}
    assert vm.by_label(samples, "ntp_clients_active", "window") == {
        "5m": 188.0,
        "1h": 1442.0,
    }


def test_by_label_skips_series_without_that_label():
    samples = {"ntp_clients_active": [s(1.0, window="5m"), s(2.0)]}
    assert vm.by_label(samples, "ntp_clients_active", "window") == {"5m": 1.0}


def test_source_offset_picks_the_named_source():
    samples = {
        "chrony_sources_last_sample_offset_seconds": [
            s(-0.04, source_name="GPS"),
            s(1.2e-9, source_name="PPS"),
            s(-0.0012, source_name="ntp1.time.nl"),
        ]
    }
    assert vm.source_offset(samples, "PPS") == 1.2e-9
    assert vm.source_offset(samples, "GPS") == -0.04
    assert vm.source_offset(samples, "nope") is None


def test_missing_metric_is_none_not_an_exception():
    assert vm.one({}, "chrony_tracking_stratum") is None
    assert vm.labels_of({}, "chrony_tracking_info") == {}
    assert vm.satellites({}) == []


def test_offset_is_plain_decimal_seconds_at_every_magnitude():
    """One unit throughout, so a column of offsets can be ranked by eye.

    Scaled units read well alone and badly in a column: 639 ns, 18.6 us and -39.95 ms
    are three scales, and a unit that changes between refreshes makes a steady figure
    look like it jumped.
    """
    assert _offset_human(6.39e-7) == "+0.000000639 s"
    assert _offset_human(-1.2e-9) == "-0.0000000012 s"
    assert _offset_human(1.86e-5) == "+0.0000186 s"
    assert _offset_human(-0.03995) == "-0.03995 s"
    assert _offset_human(2.5) == "+2.5 s"
    assert _offset_human(0.0) == "+0 s"
    assert _offset_human(None) == "unknown"


def test_offset_never_uses_exponent_notation():
    for value in (1e-12, 1e-9, 5e-7, 1.0, 1234.5):
        assert "e" not in _offset_human(value).lower()


def test_offset_keeps_four_significant_figures():
    assert _offset_human(1.2345e-6).startswith("+0.00000123")
    assert _offset_human(0.0399512) == "+0.03995 s"


def test_uptime_never_shows_more_than_two_units():
    assert _seconds_human(86400 * 17 + 3600 * 4) == "17d 4h"
    assert _seconds_human(3600 * 4 + 720) == "4h 12m"
    assert _seconds_human(720) == "12m"
    assert _seconds_human(42) == "42s"
    assert _seconds_human(-5) == "0s"


def test_leading_zeros_are_banded_two_at_a_time_alternating():
    """Counting six identical glyphs against seven is what the banding removes."""
    from app.main import _mark_zeros

    out = str(_mark_zeros("+0.0000007203"))
    assert out == (
        '+0.<span class="z0">00</span><span class="z1">00</span>'
        '<span class="z0">00</span>7203'
    )


def test_a_lone_trailing_zero_gets_its_own_band():
    from app.main import _mark_zeros

    assert str(_mark_zeros("-0.03995")) == '-0.<span class="z0">0</span>3995'


def test_numbers_without_leading_zeros_are_untouched():
    from app.main import _mark_zeros

    for text in ("+118.7", "1", "-2.5", "+6.604"):
        assert str(_mark_zeros(text)) == text


def test_banding_returns_markup_that_is_not_re_escaped():
    """Markup.__add__ escapes a plain str operand, which once ate the spans."""
    from markupsafe import Markup

    from app.main import _mark_zeros

    out = _mark_zeros("+0.0001")
    assert isinstance(out, Markup)
    assert "<span" in str(out)
    assert "&lt;span" not in str(out)


def test_offset_cell_bands_and_keeps_the_unit():
    from app.main import _offset_cell, _magnitude_cell

    assert str(_offset_cell(7.203e-7)).endswith(" s")
    assert '<span class="z0">00</span>' in str(_offset_cell(7.203e-7))
    assert str(_magnitude_cell(None)) == ""
    assert "-" not in str(_magnitude_cell(-7.203e-7))


def test_si_seconds_scales_to_the_unit_with_four_significant_figures():
    """Below "Use this time server" the figures are technical and use SI units."""
    assert _si_seconds(6.39e-7) == "+639 ns"
    assert _si_seconds(-1.2e-9) == "-1.2 ns"
    assert _si_seconds(1.86e-5) == "+18.6 µs"
    assert _si_seconds(-0.03995) == "-39.95 ms"
    assert _si_seconds(1.5) == "+1.5 s"
    assert _si_seconds(4.2e-10) == "+0.42 ns"
    assert _si_seconds(0) == "+0 s"


def test_si_seconds_unsigned_for_magnitudes():
    assert _si_seconds(-2.5e-6, sign=False) == "2.5 µs"
    assert _si_seconds(0, sign=False) == "0 s"


def test_si_seconds_steps_up_a_unit_when_rounding_reaches_1000():
    """999.96 ns must read 1 µs, not a four-digit count of nanoseconds."""
    assert _si_seconds(9.9996e-7) == "+1 µs"
    assert _si_seconds(-9.99996e-4) == "-1 ms"
