from src.actions.builtin.vrops.analysis import (
    compute_trend,
    evaluate_threshold,
    METRIC_CATALOG,
    STANDARD_METRIC_KEYS,
)


def test_compute_trend_rising():
    assert compute_trend([10, 12, 14, 16, 18, 20]) == "rising"


def test_compute_trend_falling():
    assert compute_trend([90, 80, 70, 60, 50]) == "falling"


def test_compute_trend_stable_flat():
    assert compute_trend([50, 50, 50, 50]) == "stable"


def test_compute_trend_stable_noisy():
    # small wobble around a constant, no real direction
    assert compute_trend([50, 51, 49, 50, 51, 49]) == "stable"


def test_compute_trend_single_point_is_stable():
    assert compute_trend([42]) == "stable"


def test_compute_trend_empty_is_stable():
    assert compute_trend([]) == "stable"


def test_evaluate_threshold_counts_breaches():
    breached, count = evaluate_threshold([10, 95, 50, 99], 90.0)
    assert breached is True
    assert count == 2


def test_evaluate_threshold_none_threshold():
    assert evaluate_threshold([100, 200], None) == (False, 0)


def test_evaluate_threshold_empty_samples():
    assert evaluate_threshold([], 90.0) == (False, 0)


def test_evaluate_threshold_inclusive_boundary():
    # a sample exactly at the threshold counts as a breach
    assert evaluate_threshold([90.0], 90.0) == (True, 1)


def test_catalog_keys_match_standard_list():
    expected = [
        "cpu|usage_average",
        "mem|usage_average",
        "virtualDisk|totalLatency",
        "net|usage_average",
        "disk|usage_average",
    ]
    assert STANDARD_METRIC_KEYS == expected
    assert list(METRIC_CATALOG.keys()) == expected
