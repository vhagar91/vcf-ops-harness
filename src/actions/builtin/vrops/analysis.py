"""Pure, network-free analysis helpers for the vROps tools.

Every function here is deterministic and takes plain data, so the LLM only ever
narrates a pre-computed verdict — it cannot invent trends or numbers, and large
result sets are aggregated into compact summaries that fit the context budget.
"""

from __future__ import annotations

# Alert criticality ordered most-severe first; unknown levels sort last.
ALERT_SEVERITY_ORDER = ["CRITICAL", "IMMEDIATE", "WARNING", "INFORMATION"]


def _severity_rank(level: str | None) -> int:
    try:
        return ALERT_SEVERITY_ORDER.index((level or "").upper())
    except ValueError:
        return len(ALERT_SEVERITY_ORDER)

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


def summarize_alerts(alerts: list[dict], top_n: int = 10,
                     max_name_groups: int = 10) -> dict:
    """Aggregate alerts into a compact, COMPLETE summary that fits the context
    budget: an accurate total, breakdowns by criticality and status, the most
    common alert names, and the most-severe alerts in detail.

    Returning the full alert list would be capped/truncated before the model
    sees it (see MAX_TOOL_RESULT_CHARS / MAX_TOOL_LIST_ITEMS), so the model could
    neither count nor describe a large alert set. This summary keeps the totals
    accurate regardless of how many alerts there are.
    """
    total = len(alerts)
    by_criticality: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_name: dict[str, int] = {}
    for a in alerts:
        level = (a.get("level") or "UNKNOWN").upper()
        status = (a.get("status") or "UNKNOWN").upper()
        name = a.get("name") or "(unnamed)"
        by_criticality[level] = by_criticality.get(level, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        by_name[name] = by_name.get(name, 0) + 1

    # Order the criticality breakdown most-severe first for readable narration.
    by_criticality = dict(sorted(by_criticality.items(),
                                 key=lambda kv: _severity_rank(kv[0])))
    # Keep only the most common alert names so the payload stays bounded.
    by_name = dict(sorted(by_name.items(), key=lambda kv: kv[1],
                          reverse=True)[:max_name_groups])

    # Most-severe first, then most-recent, for the detailed sample.
    ranked = sorted(alerts, key=lambda a: (_severity_rank(a.get("level")),
                                           -(a.get("startTimeUTC") or 0)))
    top = [
        {"alertId": a.get("alertId"), "name": a.get("name"),
         "level": a.get("level"), "status": a.get("status"),
         "resourceId": a.get("resourceId"),
         "resourceName": a.get("resourceName"),
         "resourceKind": a.get("resourceKind")}
        for a in ranked[:top_n]
    ]
    return {
        "total": total,
        "by_criticality": by_criticality,
        "by_status": by_status,
        "by_name": by_name,
        "top": top,
        "shown_in_top": len(top),
    }


# --- Fleet capacity / rightsizing -------------------------------------------
# Stat keys verified against the live vROps instance (OnlineCapacityAnalytics
# family). Key availability is version-dependent; re-check with get_stat_keys
# when porting to another Aria/vROps version.

# Cluster capacity is reported BY TYPE (cpu / memory / storage), not as one conflated
# number. Per-type "capacity remaining %" = capacityRemaining / usableCapacity — the
# vROps capacity-engine view (after HA/buffer). Raw utilization % is shown alongside it.
# All keys verified on the live instance.
CLUSTER_OVERALL_REMAINING_PCT_KEY = "OnlineCapacityAnalytics|capacityRemainingPercentage"
# Per-dimension remaining (absolute):
CLUSTER_CPU_REMAINING_KEY = "OnlineCapacityAnalytics|cpu|demand|capacityRemaining"      # MHz
CLUSTER_MEM_REMAINING_KEY = "OnlineCapacityAnalytics|mem|demand|capacityRemaining"      # KB
CLUSTER_DISK_REMAINING_KEY = "OnlineCapacityAnalytics|diskspace|demand|capacityRemaining"  # GB
# Per-dimension usable capacity (absolute):
CLUSTER_CPU_USABLE_KEY = "cpu|demand|usableCapacity"        # MHz
CLUSTER_MEM_USABLE_KEY = "mem|demand|usableCapacity"        # KB
CLUSTER_DISK_USABLE_KEY = "diskspace|demand|usableCapacity"  # GB
# Raw utilization % (mem|capacity_usagepct_average returns no data here; use usage_average):
CLUSTER_CPU_USAGE_PCT_KEY = "cpu|capacity_usagepct_average"
CLUSTER_MEM_USAGE_PCT_KEY = "mem|usage_average"
CLUSTER_CAPACITY_KEYS = [
    CLUSTER_OVERALL_REMAINING_PCT_KEY,
    CLUSTER_CPU_REMAINING_KEY, CLUSTER_MEM_REMAINING_KEY, CLUSTER_DISK_REMAINING_KEY,
    CLUSTER_CPU_USABLE_KEY, CLUSTER_MEM_USABLE_KEY, CLUSTER_DISK_USABLE_KEY,
    CLUSTER_CPU_USAGE_PCT_KEY, CLUSTER_MEM_USAGE_PCT_KEY,
]

# VM rightsizing — current vs vROps-recommended (verified keys):
VM_CURRENT_VCPU_KEY = "config|hardware|num_Cpu"          # vCPU count
VM_CURRENT_CPU_MHZ_KEY = "cpu|vm_capacity_provisioned"   # current CPU MHz
VM_RECOMMENDED_CPU_MHZ_KEY = "OnlineCapacityAnalytics|cpu|recommendedSize"  # MHz
VM_CURRENT_MEM_KB_KEY = "mem|guest_provisioned"          # KB
VM_RECOMMENDED_MEM_KB_KEY = "OnlineCapacityAnalytics|mem|recommendedSize"   # KB
VM_RIGHTSIZING_KEYS = [
    VM_CURRENT_VCPU_KEY,
    VM_CURRENT_CPU_MHZ_KEY,
    VM_RECOMMENDED_CPU_MHZ_KEY,
    VM_CURRENT_MEM_KB_KEY,
    VM_RECOMMENDED_MEM_KB_KEY,
]

_KB_PER_GB = 1024 * 1024


def free_capacity_score(remaining_pcts: list[float | None]) -> float | None:
    """Bottleneck free-capacity score: the *minimum* remaining percentage across
    the given dimensions (CPU/mem/disk). A cluster is constrained by its tightest
    resource, so the smallest remaining-% is the most honest single number to
    rank 'least free' by. None values (missing metric) are ignored; returns None
    when no dimension has data.
    """
    present = [p for p in remaining_pcts if p is not None]
    if not present:
        return None
    return round(min(present), 2)


def pct_remaining(remaining: float | None, usable: float | None) -> float | None:
    """Capacity-remaining percentage for one dimension = remaining / usable * 100.
    None if either value is missing or usable is non-positive."""
    if remaining is None or not usable or usable <= 0:
        return None
    return round(remaining / usable * 100.0, 1)


def oversize_score(vcpu_reclaimable: float | None,
                   mem_reclaimable_gb: float | None) -> float:
    """Rank magnitude for an oversized VM. vCPUs and memory are not directly
    comparable, so combine them into one heuristic: each reclaimable vCPU is
    weighted as ~2 GB of memory. Missing values count as 0.
    """
    vcpu = vcpu_reclaimable or 0.0
    mem = mem_reclaimable_gb or 0.0
    return round(vcpu * 2.0 + mem, 2)


def reclaimable_vcpu(num_cpu: float | None, current_cpu_mhz: float | None,
                     recommended_cpu_mhz: float | None) -> float:
    """Reclaimable vCPUs for an oversized VM. vROps gives a recommended CPU size in
    MHz, not a vCPU count, so convert via the VM's own MHz-per-vCPU ratio:
    num_cpu * (1 - recommended_mhz / current_mhz). Returns 0.0 if any input is
    missing/zero or the VM is not oversized (recommended >= current).
    """
    if not num_cpu or not current_cpu_mhz or not recommended_cpu_mhz:
        return 0.0
    if recommended_cpu_mhz >= current_cpu_mhz:
        return 0.0
    return round(num_cpu * (1.0 - recommended_cpu_mhz / current_cpu_mhz), 2)


def reclaimable_mem_gb(current_mem_kb: float | None,
                       recommended_mem_kb: float | None) -> float:
    """Reclaimable memory in GB: current provisioned minus vROps-recommended (both
    in KB). Returns 0.0 if either is missing or the VM is not oversized.
    """
    if current_mem_kb is None or recommended_mem_kb is None:
        return 0.0
    diff_kb = current_mem_kb - recommended_mem_kb
    if diff_kb <= 0:
        return 0.0
    return round(diff_kb / _KB_PER_GB, 2)


# --- VM placement ------------------------------------------------------------
# Keys to evaluate whether a requested VM fits a cluster/host. Both kinds expose
# these (verified live). Memory total: hosts publish mem|host_provisioned; clusters
# publish mem|demand|usableCapacity (and, here, mem|host_provisioned too).
PLACEMENT_CPU_CAPACITY_KEY = "cpu|capacity_provisioned"          # total CPU MHz
PLACEMENT_CPU_CORECOUNT_KEY = "cpu|corecount_provisioned"        # cores / vCPUs
PLACEMENT_CPU_FREE_KEY = "OnlineCapacityAnalytics|cpu|demand|capacityRemaining"  # free MHz
PLACEMENT_CPU_USAGE_PCT_KEY = "cpu|capacity_usagepct_average"    # raw usage %
PLACEMENT_MEM_FREE_KEY = "OnlineCapacityAnalytics|mem|demand|capacityRemaining"  # free KB
PLACEMENT_MEM_USAGE_PCT_KEY = "mem|usage_average"               # raw usage %
PLACEMENT_MEM_TOTAL_HOST_KEY = "mem|host_provisioned"           # total KB (hosts + clusters)
PLACEMENT_MEM_TOTAL_CLUSTER_KEY = "mem|demand|usableCapacity"   # total KB (clusters fallback)

PLACEMENT_KEYS = [
    PLACEMENT_CPU_CAPACITY_KEY,
    PLACEMENT_CPU_CORECOUNT_KEY,
    PLACEMENT_CPU_FREE_KEY,
    PLACEMENT_CPU_USAGE_PCT_KEY,
    PLACEMENT_MEM_FREE_KEY,
    PLACEMENT_MEM_USAGE_PCT_KEY,
    PLACEMENT_MEM_TOTAL_HOST_KEY,
    PLACEMENT_MEM_TOTAL_CLUSTER_KEY,
]


def mhz_per_vcpu(cpu_capacity_mhz: float | None, corecount: float | None) -> float | None:
    """MHz per vCPU = total CPU MHz / core count. None if either is missing or
    corecount is non-positive."""
    if cpu_capacity_mhz is None or not corecount or corecount <= 0:
        return None
    return cpu_capacity_mhz / corecount


def headroom_after_pct(free: float | None, required: float | None,
                       total: float | None) -> float | None:
    """Headroom remaining as a percent of total after subtracting `required` from
    `free`. None if total is missing/non-positive or free/required is missing. May
    be negative (the request would overcommit the resource)."""
    if free is None or required is None or not total or total <= 0:
        return None
    return round((free - required) / total * 100.0, 1)
