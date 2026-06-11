"""Composite fleet-report tools. Each does scope + aggregate + rank in Python and
returns ONE compact ranked report, so the model makes a single call and only
narrates — same philosophy as vrops_diagnose.
"""

from __future__ import annotations

from ....config.types import ActionDefinition, ActionResult
from ....config.settings import load_config
from .actions import _build_client
from .sites import SiteMap
from .fleet import build_rows, UnknownLocation
from .analysis import (
    _KB_PER_GB,
    CLUSTER_CAPACITY_KEYS,
    CLUSTER_CAPACITY_REMAINING_PCT_KEY,
    CLUSTER_CPU_USAGE_PCT_KEY,
    CLUSTER_MEM_USAGE_PCT_KEY,
    CLUSTER_CPU_REMAINING_KEY,
    CLUSTER_MEM_REMAINING_KEY,
    CLUSTER_DISK_REMAINING_KEY,
    VM_RIGHTSIZING_KEYS,
    VM_CURRENT_VCPU_KEY,
    VM_CURRENT_CPU_MHZ_KEY,
    VM_RECOMMENDED_CPU_MHZ_KEY,
    VM_CURRENT_MEM_KB_KEY,
    VM_RECOMMENDED_MEM_KB_KEY,
    free_capacity_score,
    oversize_score,
    reclaimable_vcpu,
    reclaimable_mem_gb,
)

def _site_map() -> SiteMap:
    return SiteMap.from_file(load_config().vrops_site_map_file)


def _num(stats: dict, key: str):
    """Latest stat value as a number, or None if absent/non-numeric."""
    v = stats.get(key)
    return v if isinstance(v, (int, float)) else None


def _unknown_location_result(e: UnknownLocation) -> ActionResult:
    known = ", ".join(e.known) or "(none configured)"
    return ActionResult(
        success=False,
        summary=f"Unknown location '{e.location}'. Known sites: {known}.",
    )


def _coerce_bool(value, default: bool = True) -> bool:
    """Coerce a tool argument to bool, tolerating the string "false"/"0"/"no"
    (a plain bool(value) would treat the non-empty string "false" as True)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


async def _vrops_cluster_capacity_report(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
    except Exception as e:
        return ActionResult(success=False, summary=str(e))
    try:
        location = args.get("location")
        top_n = int(args.get("top_n", 5))
        sort = args.get("sort", "least_free")
        rows = build_rows(client, _site_map(), location,
                          "ClusterComputeResource", CLUSTER_CAPACITY_KEYS)

        scored = []
        for r in rows:
            s = r["stats"]
            cpu_usage = _num(s, CLUSTER_CPU_USAGE_PCT_KEY)
            mem_usage = _num(s, CLUSTER_MEM_USAGE_PCT_KEY)
            # Primary metric: vROps' own overall capacity-remaining %. Fallback
            # when absent: bottleneck of (100 - usage%) across cpu/mem.
            score = _num(s, CLUSTER_CAPACITY_REMAINING_PCT_KEY)
            if score is None:
                score = free_capacity_score([
                    None if cpu_usage is None else 100.0 - cpu_usage,
                    None if mem_usage is None else 100.0 - mem_usage,
                ])
            if score is None:
                continue
            scored.append({
                "cluster": r["name"],
                "free_capacity_pct": round(score, 2),
                "cpu_usage_pct": cpu_usage,
                "mem_usage_pct": mem_usage,
                "cpu_remaining": _num(s, CLUSTER_CPU_REMAINING_KEY),
                "mem_remaining": _num(s, CLUSTER_MEM_REMAINING_KEY),
                "disk_remaining": _num(s, CLUSTER_DISK_REMAINING_KEY),
                "health": r["health"],
            })

        if not scored:
            scope = f" in {location}" if location else ""
            return ActionResult(success=True,
                                summary=f"No cluster capacity data available{scope}.",
                                raw={"clusters": [], "location": location})

        reverse = (sort == "most_free")
        scored.sort(key=lambda c: c["free_capacity_pct"], reverse=reverse)
        top = scored[:top_n]
        leader = top[0]
        descriptor = "most" if reverse else "fewest"
        headline = (f"{leader['cluster']} has the {descriptor} free capacity"
                    + (f" in {location}" if location else "")
                    + f" ({leader['free_capacity_pct']}% remaining).")
        return ActionResult(success=True, summary=headline,
                            raw={"location": location, "sort": sort,
                                 "shown": len(top), "total_clusters": len(scored),
                                 "clusters": top})
    except UnknownLocation as e:
        return _unknown_location_result(e)
    except Exception as e:
        return ActionResult(success=False,
                            summary=f"Cluster capacity report failed: {e}")


async def _vrops_oversized_vms_report(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
    except Exception as e:
        return ActionResult(success=False, summary=str(e))
    try:
        location = args.get("location")
        top_n = int(args.get("top_n", 20))
        min_reclaimable = args.get("min_reclaimable")
        min_floor = float(min_reclaimable) if min_reclaimable is not None else None
        rows = build_rows(client, _site_map(), location,
                          "VirtualMachine", VM_RIGHTSIZING_KEYS)

        oversized = []
        for r in rows:
            s = r["stats"]
            num_cpu = _num(s, VM_CURRENT_VCPU_KEY)
            cur_mhz = _num(s, VM_CURRENT_CPU_MHZ_KEY)
            rec_mhz = _num(s, VM_RECOMMENDED_CPU_MHZ_KEY)
            cur_mem = _num(s, VM_CURRENT_MEM_KB_KEY)
            rec_mem = _num(s, VM_RECOMMENDED_MEM_KB_KEY)
            rec_vcpu = reclaimable_vcpu(num_cpu, cur_mhz, rec_mhz)
            rec_mem_gb = reclaimable_mem_gb(cur_mem, rec_mem)
            score = oversize_score(rec_vcpu, rec_mem_gb)
            if score <= 0:
                continue
            if min_floor is not None and score < min_floor:
                continue
            oversized.append({
                "vm": r["name"],
                "oversize_score": score,
                "current_vcpu": num_cpu,
                "reclaimable_vcpu": rec_vcpu,
                "current_mem_gb": round(cur_mem / _KB_PER_GB, 2) if cur_mem is not None else None,
                "reclaimable_mem_gb": rec_mem_gb,
            })

        if not oversized:
            scope = f" in {location}" if location else ""
            return ActionResult(success=True,
                                summary=f"No oversized VMs detected{scope}.",
                                raw={"vms": [], "location": location})

        oversized.sort(key=lambda v: v["oversize_score"], reverse=True)
        top = oversized[:top_n]
        headline = (f"{len(oversized)} oversized VM(s)"
                    + (f" in {location}" if location else "")
                    + f"; top {len(top)} by reclaimable capacity.")
        return ActionResult(success=True, summary=headline,
                            raw={"location": location, "shown": len(top),
                                 "total_oversized": len(oversized), "vms": top})
    except UnknownLocation as e:
        return _unknown_location_result(e)
    except Exception as e:
        return ActionResult(success=False,
                            summary=f"Oversized-VM report failed: {e}")


async def _vrops_fleet_query(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
    except Exception as e:
        return ActionResult(success=False, summary=str(e))
    try:
        resource_kind = args.get("resource_kind")
        if not resource_kind:
            return ActionResult(success=False, summary="resource_kind is required.")
        location = args.get("location")
        stat_keys = args.get("stat_keys") or []
        sort_by = args.get("sort_by")
        if sort_by and sort_by not in stat_keys:
            return ActionResult(
                success=False,
                summary=f"sort_by '{sort_by}' must be one of stat_keys {stat_keys}.",
            )
        top_n = int(args.get("top_n", 10))
        descending = _coerce_bool(args.get("descending"), default=True)

        rows = build_rows(client, _site_map(), location, resource_kind, stat_keys)
        total = len(rows)
        if sort_by:
            rows = [r for r in rows if isinstance(r["stats"].get(sort_by), (int, float))]
            rows.sort(key=lambda r: r["stats"][sort_by], reverse=descending)
        top = rows[:top_n]
        return ActionResult(success=True,
                            summary=(f"{total} {resource_kind} resource(s)"
                                     + (f" in {location}" if location else "")
                                     + f"; showing {len(top)}."),
                            raw={"resource_kind": resource_kind, "location": location,
                                 "sort_by": sort_by, "shown": len(top),
                                 "total": total, "ranked": len(rows), "rows": top})
    except UnknownLocation as e:
        return _unknown_location_result(e)
    except Exception as e:
        return ActionResult(success=False, summary=f"Fleet query failed: {e}")


vrops_report_actions: list[ActionDefinition] = [
    ActionDefinition(
        name="vrops_cluster_capacity_report",
        description=(
            "Rank clusters by free capacity across a site or the whole estate in ONE "
            "call. Use for 'which cluster has the most/least free resources', "
            "'capacity report', or 'where can I place new workloads'. Optionally filter "
            "by physical location (e.g. 'Madrid'). Returns a ranked report; narrate the "
            "leader and the notable rows."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Physical site to filter by, e.g. 'Madrid'. Omit for all sites."},
                "top_n": {"type": "integer", "description": "How many clusters to return", "default": 5},
                "sort": {"type": "string", "enum": ["least_free", "most_free"], "default": "least_free"},
            },
        },
        handler=_vrops_cluster_capacity_report,
    ),
    ActionDefinition(
        name="vrops_oversized_vms_report",
        description=(
            "List oversized VMs (vROps native rightsizing) ranked by reclaimable "
            "capacity, in ONE call. Use for 'oversized VMs', 'VMs sobredimensionadas', "
            "'rightsizing report', or 'where can I reclaim CPU/RAM'. Optionally filter by "
            "physical location. Returns a ranked report with reclaimable vCPU/memory."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Physical site to filter by. Omit for all sites."},
                "top_n": {"type": "integer", "description": "How many VMs to return", "default": 20},
                "min_reclaimable": {"type": "number", "description": "Only VMs with an oversize score at or above this floor"},
            },
        },
        handler=_vrops_oversized_vms_report,
    ),
    ActionDefinition(
        name="vrops_fleet_query",
        description=(
            "Generic fleet query for ad-hoc 'rank all X by metric Y' questions not "
            "covered by the capacity or oversized-VM reports. Enumerate a resource kind "
            "(optionally filtered by location), fetch the given stat keys, and rank by "
            "one of them. Prefer the dedicated reports when they fit."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "resource_kind": {"type": "string", "description": "e.g. HostSystem, Datastore, VirtualMachine, ClusterComputeResource"},
                "location": {"type": "string", "description": "Physical site to filter by. Omit for all sites."},
                "stat_keys": {"type": "array", "items": {"type": "string"}, "description": "Stat keys to fetch, e.g. ['cpu|capacity_usagepct_average']"},
                "sort_by": {"type": "string", "description": "Stat key to rank by (must be one of stat_keys)"},
                "descending": {"type": "boolean", "description": "Highest first", "default": True},
                "top_n": {"type": "integer", "description": "How many rows to return", "default": 10},
            },
            "required": ["resource_kind"],
        },
        handler=_vrops_fleet_query,
    ),
]
