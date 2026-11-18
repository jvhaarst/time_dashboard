"""Unit tests for the parts that turn VictoriaMetrics output into page figures.

These are the places where a wrong assumption produces a plausible-looking page rather
than an error, which is why they are the parts under test.
"""

from app import vm
from app.main import _offset_human, _seconds_human


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


def test_offset_scales_to_the_magnitude_and_keeps_the_sign():
    assert _offset_human(6.39e-7) == "+639 ns"
    assert _offset_human(-1.2e-9) == "-1 ns"
    assert _offset_human(1.86e-5) == "+18.6 µs"
    assert _offset_human(-0.03995) == "-39.95 ms"
    assert _offset_human(2.5) == "+2.500 s"
    assert _offset_human(None) == "unknown"


def test_uptime_never_shows_more_than_two_units():
    assert _seconds_human(86400 * 17 + 3600 * 4) == "17d 4h"
    assert _seconds_human(3600 * 4 + 720) == "4h 12m"
    assert _seconds_human(720) == "12m"
    assert _seconds_human(42) == "42s"
    assert _seconds_human(-5) == "0s"
