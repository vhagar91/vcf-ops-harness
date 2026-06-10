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


def summarize_metric(key: str, samples: list[float]) -> dict:
    """Build the compact per-metric verdict for one stat key."""
    meta = METRIC_CATALOG.get(key, {"label": key, "threshold": None, "unit": ""})
    threshold = meta["threshold"]
    if not samples:
        return {
            "key": key, "label": meta["label"], "unit": meta["unit"],
            "samples": 0, "latest": None, "avg": None, "min": None, "max": None,
            "trend": "stable", "threshold": threshold,
            "breached": False, "breach_count": 0,
        }
    breached, breach_count = evaluate_threshold(samples, threshold)
    return {
        "key": key, "label": meta["label"], "unit": meta["unit"],
        "samples": len(samples),
        "latest": round(samples[-1], 2),
        "avg": round(sum(samples) / len(samples), 2),
        "min": round(min(samples), 2),
        "max": round(max(samples), 2),
        "trend": compute_trend(samples),
        "threshold": threshold,
        "breached": breached,
        "breach_count": breach_count,
    }


def build_recommendations(health_state: str | None, alerts: list[dict],
                          metrics: list[dict]) -> list[str]:
    """Map detected conditions to ranked, canned remediation suggestions."""
    recs: list[str] = []
    crit = [a for a in alerts if (a.get("level") or "").upper() in ("CRITICAL", "IMMEDIATE")]
    for a in crit:
        recs.append(f"Investigate critical alert: {a.get('name') or a.get('alertId')}.")

    by_key = {m["key"]: m for m in metrics}
    cpu = by_key.get("cpu|usage_average")
    if cpu and cpu["breached"]:
        if cpu["trend"] == "rising":
            recs.append("CPU is sustained high and climbing; investigate a runaway "
                        "process or add vCPU capacity.")
        else:
            recs.append("CPU is sustained high; review workload sizing or add vCPU capacity.")
    mem = by_key.get("mem|usage_average")
    if mem and mem["breached"]:
        recs.append("Memory usage is high; check for leaks/ballooning or add RAM.")
    disk = by_key.get("virtualDisk|totalLatency")
    if disk and disk["breached"]:
        recs.append("Disk latency is elevated; check datastore contention or the storage backend.")

    if not recs:
        if alerts:
            recs.append("Active alerts present but no threshold breaches detected; "
                        "review the alerts and recent changes.")
        elif (health_state or "").upper() in ("RED", "ORANGE", "YELLOW"):
            recs.append("Health is degraded but no threshold breaches detected; "
                        "review active alerts and recent changes.")
        else:
            recs.append("No action needed; resource is healthy.")
    return recs


def rollup_verdict(health_state: str | None, alerts: list[dict],
                   metrics: list[dict]) -> str:
    """Overall verdict: OK | WARNING | CRITICAL."""
    state = (health_state or "").upper()
    levels = {(a.get("level") or "").upper() for a in alerts}
    if state == "RED" or "CRITICAL" in levels or "IMMEDIATE" in levels:
        return "CRITICAL"
    breached = any(m["breached"] for m in metrics)
    if state in ("ORANGE", "YELLOW") or breached or "WARNING" in levels:
        return "WARNING"
    return "OK"
