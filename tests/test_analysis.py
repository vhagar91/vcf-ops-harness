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


from src.actions.builtin.vrops.analysis import (
    summarize_metric,
    build_recommendations,
    rollup_verdict,
)


def test_summarize_metric_basic():
    m = summarize_metric("cpu|usage_average", [80, 85, 91, 95])
    assert m["key"] == "cpu|usage_average"
    assert m["label"] == "CPU %"
    assert m["latest"] == 95.0
    assert m["avg"] == 87.75
    assert m["min"] == 80.0
    assert m["max"] == 95.0
    assert m["trend"] == "rising"
    assert m["threshold"] == 90.0
    assert m["breached"] is True
    assert m["breach_count"] == 2
    assert m["samples"] == 4


def test_summarize_metric_empty_series():
    m = summarize_metric("mem|usage_average", [])
    assert m["samples"] == 0
    assert m["latest"] is None
    assert m["trend"] == "stable"
    assert m["breached"] is False
    assert m["breach_count"] == 0


def test_summarize_metric_unknown_key_uses_fallback_meta():
    m = summarize_metric("weird|key", [1, 2, 3])
    assert m["label"] == "weird|key"
    assert m["threshold"] is None
    assert m["breached"] is False


def test_build_recommendations_critical_alert_first():
    alerts = [{"level": "CRITICAL", "name": "Host down", "alertId": "a1"}]
    recs = build_recommendations("RED", alerts, [])
    assert "Host down" in recs[0]


def test_build_recommendations_cpu_high_rising():
    metrics = [summarize_metric("cpu|usage_average", [88, 92, 95, 98])]
    recs = build_recommendations("YELLOW", [], metrics)
    assert any("CPU" in r and "climbing" in r for r in recs)


def test_build_recommendations_memory_breach():
    metrics = [summarize_metric("mem|usage_average", [95, 96, 97])]
    recs = build_recommendations("YELLOW", [], metrics)
    assert any("Memory" in r or "memory" in r for r in recs)


def test_build_recommendations_healthy_no_action():
    metrics = [summarize_metric("cpu|usage_average", [10, 12, 11])]
    recs = build_recommendations("GREEN", [], metrics)
    assert recs == ["No action needed; resource is healthy."]


def test_rollup_verdict_critical_on_red_health():
    assert rollup_verdict("RED", [], []) == "CRITICAL"


def test_rollup_verdict_critical_on_critical_alert():
    assert rollup_verdict("GREEN", [{"level": "CRITICAL"}], []) == "CRITICAL"


def test_rollup_verdict_warning_on_breach():
    metrics = [summarize_metric("cpu|usage_average", [95, 96])]
    assert rollup_verdict("GREEN", [], metrics) == "WARNING"


def test_rollup_verdict_ok_when_clean():
    metrics = [summarize_metric("cpu|usage_average", [10, 12])]
    assert rollup_verdict("GREEN", [], metrics) == "OK"
