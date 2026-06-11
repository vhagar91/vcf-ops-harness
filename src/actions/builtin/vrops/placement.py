"""vrops_placement_recommendation — given a requested VM size (vCPU + memory GB),
recommend the best cluster and host to place it on, in one call.

Two-stage: rank fitting clusters in the location, pick the best, then rank the
hosts beneath it. Fit uses the vROps capacity-engine view (post-HA/buffer); raw
headroom is reported alongside. 'Best' = most bottleneck headroom after placement.
"""

from __future__ import annotations

from ....config.types import ActionDefinition, ActionResult
from .actions import _build_client
from .fleet import build_rows, collect_descendants, attach_stats, UnknownLocation
from .reports import _site_map, _num, _unknown_location_result
from .analysis import (
    PLACEMENT_KEYS,
    PLACEMENT_CPU_CAPACITY_KEY,
    PLACEMENT_CPU_CORECOUNT_KEY,
    PLACEMENT_CPU_FREE_KEY,
    PLACEMENT_CPU_USAGE_PCT_KEY,
    PLACEMENT_MEM_FREE_KEY,
    PLACEMENT_MEM_USAGE_PCT_KEY,
    PLACEMENT_MEM_TOTAL_HOST_KEY,
    PLACEMENT_MEM_TOTAL_CLUSTER_KEY,
    mhz_per_vcpu,
    headroom_after_pct,
    free_capacity_score,
)

_KB_PER_GB = 1024 * 1024


def _evaluate(stats: dict, vcpu: int, memory_gb: float) -> dict:
    """Evaluate one cluster/host as a placement target for a vcpu/memory_gb VM.

    Fit decision uses the vROps capacity-engine free capacity; raw headroom
    (total - usage) is computed for context only. Returns cpu/memory breakdowns,
    an overall `fits` flag, and `headroom_after_pct` = the bottleneck (min of
    cpu/mem) headroom remaining after placement, used to rank candidates.
    """
    required_mem_kb = memory_gb * _KB_PER_GB

    cpu_total = _num(stats, PLACEMENT_CPU_CAPACITY_KEY)
    cores = _num(stats, PLACEMENT_CPU_CORECOUNT_KEY)
    ratio = mhz_per_vcpu(cpu_total, cores)
    cpu_free = _num(stats, PLACEMENT_CPU_FREE_KEY)
    cpu_usage = _num(stats, PLACEMENT_CPU_USAGE_PCT_KEY)
    required_mhz = vcpu * ratio if ratio is not None else None
    cpu_fits = (cpu_free is not None and required_mhz is not None and cpu_free >= required_mhz)
    cpu_free_after = (cpu_free - required_mhz) if (cpu_free is not None and required_mhz is not None) else None
    cpu_raw_free = (cpu_total * (1 - cpu_usage / 100.0)) if (cpu_total is not None and cpu_usage is not None) else None
    cpu_head = headroom_after_pct(cpu_free, required_mhz, cpu_total)

    mem_free = _num(stats, PLACEMENT_MEM_FREE_KEY)
    mem_total = _num(stats, PLACEMENT_MEM_TOTAL_HOST_KEY)
    if mem_total is None:
        mem_total = _num(stats, PLACEMENT_MEM_TOTAL_CLUSTER_KEY)
    mem_usage = _num(stats, PLACEMENT_MEM_USAGE_PCT_KEY)
    mem_fits = (mem_free is not None and mem_free >= required_mem_kb)
    mem_free_after = (mem_free - required_mem_kb) if mem_free is not None else None
    mem_raw_free = (mem_total * (1 - mem_usage / 100.0)) if (mem_total is not None and mem_usage is not None) else None
    mem_head = headroom_after_pct(mem_free, required_mem_kb, mem_total)

    return {
        "fits": bool(cpu_fits and mem_fits),
        "headroom_after_pct": free_capacity_score([cpu_head, mem_head]),
        "cpu": {
            "free_mhz": round(cpu_free, 1) if cpu_free is not None else None,
            "required_mhz": round(required_mhz, 1) if required_mhz is not None else None,
            "free_after_mhz": round(cpu_free_after, 1) if cpu_free_after is not None else None,
            "raw_free_mhz": round(cpu_raw_free, 1) if cpu_raw_free is not None else None,
            "fits": bool(cpu_fits),
        },
        "memory": {
            "free_gb": round(mem_free / _KB_PER_GB, 2) if mem_free is not None else None,
            "required_gb": memory_gb,
            "free_after_gb": round(mem_free_after / _KB_PER_GB, 2) if mem_free_after is not None else None,
            "raw_free_gb": round(mem_raw_free / _KB_PER_GB, 2) if mem_raw_free is not None else None,
            "fits": bool(mem_fits),
        },
    }
