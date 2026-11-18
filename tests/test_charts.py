"""Tests for the SVG renderers: the shapes that must survive thinning and scaling."""

from app import charts


def test_empty_series_renders_a_message_not_a_broken_chart():
    svg = charts.offset_chart([], [], [], 0, 100)
    assert "chart empty" in svg
    assert "no data in this window" in svg


def test_bounds_always_keep_zero_in_view():
    """A flat 50 ns trace must not be auto-scaled into a mountain range."""
    lo, hi = charts._symmetric_bounds([5e-8, 5.1e-8, 4.9e-8])
    assert lo <= 0 <= hi


def test_bounds_never_collapse_to_a_single_line():
    lo, hi = charts._symmetric_bounds([0.0, 0.0])
    assert hi > lo


def test_bounds_of_nothing_are_finite():
    lo, hi = charts._symmetric_bounds([])
    assert lo < hi


def test_decimate_caps_the_length_and_keeps_both_ends():
    points = [(float(i), float(i)) for i in range(5000)]
    out = charts._decimate(points)
    assert len(out) <= charts.MAX_POINTS + 1
    assert out[0] == points[0]
    assert out[-1] == points[-1]


def test_decimate_leaves_a_short_series_alone():
    points = [(1.0, 1.0), (2.0, 2.0)]
    assert charts._decimate(points) == points


def test_calendar_marks_a_day_without_data_as_none_not_bad():
    svg = charts.calendar(
        [
            {"timestamp": 1793491200, "availability": None, "samples": 0},
            {"timestamp": 1793577600, "availability": 0.5, "samples": 10},
        ]
    )
    assert "cal-none" in svg
    assert "no data" in svg
    assert "cal-bad" in svg


def test_calendar_buckets_on_the_documented_thresholds():
    def klass(avail):
        svg = charts.calendar([{"timestamp": 0, "availability": avail, "samples": 1}])
        for name in ("cal-good", "cal-ok", "cal-bad"):
            if name in svg:
                return name
        return "none"

    assert klass(1.0) == "cal-good"
    assert klass(0.999) == "cal-good"
    assert klass(0.9989) == "cal-ok"
    assert klass(0.99) == "cal-ok"
    assert klass(0.98) == "cal-bad"


def test_adev_chart_needs_two_points():
    assert "not enough history yet" in charts.adev_chart([(60.0, 1e-9)])
    assert "polyline" in charts.adev_chart([(60.0, 1e-9), (120.0, 5e-10)])


def test_adev_chart_drops_non_positive_values_that_a_log_axis_cannot_show():
    svg = charts.adev_chart([(60.0, 0.0), (120.0, 1e-9), (240.0, 2e-9)])
    assert "polyline" in svg


def test_satellite_chart_labels_each_constellation_once():
    sats = [
        {"constellation": "GPS", "gnssid": "0", "prn": "1", "ss": 45.0, "used": True},
        {"constellation": "GPS", "gnssid": "0", "prn": "2", "ss": 40.0, "used": False},
        {"constellation": "Galileo", "gnssid": "2", "prn": "9", "ss": 42.0, "used": True},
    ]
    svg = charts.satellite_chart(sats)
    assert svg.count(">GPS</text>") == 1
    assert svg.count(">Galileo</text>") == 1
    assert svg.count('class="sat-used"') == 2
    assert svg.count('class="sat-seen"') == 1


def test_titles_are_escaped():
    """Source names come from the metrics store and land inside SVG markup."""
    svg = charts.peers_chart(
        [{"name": '<script>x</script>', "points": [(0.0, 1.0), (1.0, 2.0)]}], 0, 1
    )
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
