"""Pure, network-free analysis helpers for vrops_diagnose.

Every function here is deterministic and takes plain data, so the LLM only ever
narrates a pre-computed verdict — it cannot invent trends or numbers.
"""

from __future__ import annotations

# Default per-metric thresholds, keyed by vROps stat key. A sample breaches when
# it reaches or exceeds `threshold`. `threshold=None` means no threshold is defined.
METRIC_CATALOG: dict[str, dict] = {
    "cpu|usage_average":        {"label": "CPU %",           "threshold": 90.0, "unit": "%"},
    "mem|usage_average":        {"label": "Memory %",        "threshold": 90.0, "unit": "%"},
    "virtualDisk|totalLatency": {"label": "Disk latency",    "threshold": 20.0, "unit": "ms"},
    "net|usage_average":        {"label": "Network",         "threshold": None, "unit": "KBps"},
    "disk|usage_average":       {"label": "Disk throughput", "threshold": None, "unit": "KBps"},
}

STANDARD_METRIC_KEYS = list(METRIC_CATALOG.keys())


def compute_trend(samples: list[float], rel_tol: float = 0.05) -> str:
    """Classify a series as 'rising' | 'falling' | 'stable' via least-squares slope.

    The total modeled change across the window (slope * span) is compared against
    `rel_tol` fraction of the series mean; within that counts as 'stable'.
    Fewer than 2 points -> 'stable'.
    """
    n = len(samples)
    if n < 2:
        return "stable"
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(samples) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return "stable"
    slope = sum((xs[i] - mean_x) * (samples[i] - mean_y) for i in range(n)) / denom
    total_change = slope * (n - 1)
    threshold = max(abs(mean_y) * rel_tol, 1e-9)
    if abs(total_change) < threshold:
        return "stable"
    return "rising" if total_change > 0 else "falling"


def evaluate_threshold(samples: list[float], threshold: float | None) -> tuple[bool, int]:
    """Return (breached, breach_count): how many samples are at or above the threshold.

    A None threshold (no limit defined) or empty series -> (False, 0).
    """
    if threshold is None or not samples:
        return (False, 0)
    count = sum(1 for s in samples if s >= threshold)
    return (count > 0, count)
