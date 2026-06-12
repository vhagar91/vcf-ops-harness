"""vrops_placement_recommendation — given a requested VM size (vCPU + memory GB),
recommend the best host to place it on, in one call.

Evaluates candidate hosts across all clusters in scope (a site, or the whole
estate) and picks the host with the most bottleneck headroom remaining after the
VM lands. Fit uses the vROps capacity-engine view (post-HA/buffer); raw headroom is
reported alongside. The chosen host's cluster is reported for context.
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

    # Fit + ranking use RAW headroom (total - usage%). The vROps capacity-engine
    # number (capacityRemaining) bakes in HA/buffer reservations and is frequently 0
    # across an entire cluster, which would make placement say "nothing fits" even
    # when hosts have ample real free memory — so it's reported as a caveat only.
    cpu_total = _num(stats, PLACEMENT_CPU_CAPACITY_KEY)
    cores = _num(stats, PLACEMENT_CPU_CORECOUNT_KEY)
    ratio = mhz_per_vcpu(cpu_total, cores)
    cpu_usage = _num(stats, PLACEMENT_CPU_USAGE_PCT_KEY)
    cpu_engine_free = _num(stats, PLACEMENT_CPU_FREE_KEY)
    required_mhz = vcpu * ratio if ratio is not None else None
    cpu_free = (cpu_total * (1 - cpu_usage / 100.0)) if (cpu_total is not None and cpu_usage is not None) else None
    cpu_fits = (cpu_free is not None and required_mhz is not None and cpu_free >= required_mhz)
    cpu_free_after = (cpu_free - required_mhz) if (cpu_free is not None and required_mhz is not None) else None
    cpu_head = headroom_after_pct(cpu_free, required_mhz, cpu_total)

    mem_total = _num(stats, PLACEMENT_MEM_TOTAL_HOST_KEY)
    if mem_total is None:
        mem_total = _num(stats, PLACEMENT_MEM_TOTAL_CLUSTER_KEY)
    mem_usage = _num(stats, PLACEMENT_MEM_USAGE_PCT_KEY)
    mem_engine_free = _num(stats, PLACEMENT_MEM_FREE_KEY)
    mem_free = (mem_total * (1 - mem_usage / 100.0)) if (mem_total is not None and mem_usage is not None) else None
    mem_fits = (mem_free is not None and mem_free >= required_mem_kb)
    mem_free_after = (mem_free - required_mem_kb) if mem_free is not None else None
    mem_head = headroom_after_pct(mem_free, required_mem_kb, mem_total)

    # True when vROps reserves more than raw usage shows (HA/buffer) — a caveat to surface.
    ha_reserved = bool(
        (mem_engine_free is not None and mem_free is not None and mem_engine_free < mem_free)
        or (cpu_engine_free is not None and cpu_free is not None and cpu_engine_free < cpu_free)
    )

    return {
        "fits": bool(cpu_fits and mem_fits),
        "headroom_after_pct": free_capacity_score([cpu_head, mem_head]),
        "ha_reserved": ha_reserved,
        "cpu": {
            "free_mhz": round(cpu_free, 1) if cpu_free is not None else None,
            "required_mhz": round(required_mhz, 1) if required_mhz is not None else None,
            "free_after_mhz": round(cpu_free_after, 1) if cpu_free_after is not None else None,
            "capacity_engine_free_mhz": round(cpu_engine_free, 1) if cpu_engine_free is not None else None,
            "fits": bool(cpu_fits),
        },
        "memory": {
            "free_gb": round(mem_free / _KB_PER_GB, 2) if mem_free is not None else None,
            "required_gb": memory_gb,
            "free_after_gb": round(mem_free_after / _KB_PER_GB, 2) if mem_free_after is not None else None,
            "capacity_engine_free_gb": round(mem_engine_free / _KB_PER_GB, 2) if mem_engine_free is not None else None,
            "fits": bool(mem_fits),
        },
    }


def _rank_key(c: dict):
    """Sort key: fitting candidates first, then most headroom-after-placement."""
    score = c.get("headroom_after_pct")
    return (1 if c.get("fits") else 0, score if score is not None else float("-inf"))


def _blockers(candidate: dict) -> list[str]:
    out = []
    if not candidate["cpu"]["fits"]:
        out.append("cpu")
    if not candidate["memory"]["fits"]:
        out.append("memory")
    return out


async def _vrops_placement_recommendation(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
    except Exception as e:
        return ActionResult(success=False, summary=str(e))
    try:
        vcpu = args.get("vcpu")
        memory_gb = args.get("memory_gb")
        if vcpu is None or memory_gb is None:
            return ActionResult(success=False, summary="vcpu and memory_gb are required.")
        vcpu = int(float(vcpu))
        memory_gb = float(memory_gb)
        if vcpu <= 0 or memory_gb <= 0:
            return ActionResult(success=False, summary="vcpu and memory_gb must be positive.")
        location = args.get("location")
        top_n = int(float(args.get("top_n", 3)))

        # Stage 1: clusters in scope (used to scope the host search and for context).
        cl_rows = build_rows(client, _site_map(), location,
                             "ClusterComputeResource", PLACEMENT_KEYS)
        if not cl_rows:
            scope = f" in {location}" if location else ""
            return ActionResult(success=True,
                                summary=f"No clusters found{scope} to place the VM.",
                                raw={"location": location,
                                     "request": {"vcpu": vcpu, "memory_gb": memory_gb},
                                     "recommended": {"cluster": None, "host": None, "fits": False},
                                     "candidates": []})
        # Only id/name are needed — hosts are evaluated across all clusters below.
        clusters = [{"cluster": r["name"], "id": r["id"]} for r in cl_rows]

        # Stage 2: evaluate hosts across ALL in-scope clusters, then pick the best
        # host globally. Drilling only into the top-ranked cluster could hide a
        # fitting host in another cluster when the top cluster's hosts are each too
        # small individually (fragmented aggregate capacity).
        candidates = []
        for cl in clusters:
            host_rows = attach_stats(
                client,
                collect_descendants(client, [cl["id"]], "HostSystem"),
                PLACEMENT_KEYS,
            )
            for r in host_rows:
                ev = _evaluate(r["stats"], vcpu, memory_gb)
                candidates.append({"host": r["name"], "cluster": cl["cluster"], **ev})
        candidates.sort(key=_rank_key, reverse=True)
        top = candidates[:top_n]

        rec_host = top[0] if (top and top[0]["fits"]) else None
        req = {"vcpu": vcpu, "memory_gb": memory_gb}
        loc_txt = f" in {location}" if location else ""

        if rec_host is not None:
            hp = rec_host["headroom_after_pct"]
            hp_txt = f"{hp}" if hp is not None else "n/a"
            caveat = ""
            if rec_host.get("ha_reserved"):
                eng = rec_host["memory"]["capacity_engine_free_gb"]
                caveat = (f" Note: VCF Ops' capacity engine reserves memory after HA/buffer "
                          f"(engine-free {eng} GB) — this uses raw free headroom.")
            headline = (f"Place the {vcpu} vCPU / {memory_gb:g} GB VM on "
                        f"{rec_host['host']} (cluster {rec_host['cluster']}){loc_txt}. "
                        f"After placement, bottleneck headroom ~{hp_txt}% "
                        f"(cpu free {rec_host['cpu']['free_after_mhz']} MHz, "
                        f"mem free {rec_host['memory']['free_after_gb']} GB).{caveat}")
            recommended = {"cluster": rec_host["cluster"], "host": rec_host["host"], "fits": True}
        elif top:
            closest = top[0]
            blockers = ", ".join(_blockers(closest)) or "capacity"
            headline = (f"No host can fit the {vcpu} vCPU / {memory_gb:g} GB VM{loc_txt} "
                        f"— blocked by {blockers}. Closest: {closest['host']} "
                        f"(cluster {closest['cluster']}; cpu free {closest['cpu']['free_mhz']} MHz, "
                        f"mem free {closest['memory']['free_gb']} GB).")
            recommended = {"cluster": closest["cluster"], "host": None, "fits": False}
        else:
            headline = f"No hosts found to evaluate{loc_txt}."
            recommended = {"cluster": clusters[0]["cluster"], "host": None, "fits": False}

        return ActionResult(success=True, summary=headline,
                            raw={"request": req, "location": location,
                                 "recommended": recommended,
                                 "note": ("free_* / fit use RAW headroom (total - usage); "
                                          "capacity_engine_free_* is the vROps capacity-engine "
                                          "view after HA/buffer, shown as a caveat."),
                                 "candidates": top})
    except UnknownLocation as e:
        return _unknown_location_result(e)
    except Exception as e:
        return ActionResult(success=False, summary=f"Placement recommendation failed: {e}")


vrops_placement_action = ActionDefinition(
    name="vrops_placement_recommendation",
    description=(
        "Recommend where to place a new VM of a given size in ONE call. Use for "
        "'where should I place / put a VM of N vCPU and M GB', 'best host/cluster for "
        "a new VM', 'capacity to host a VM'. Evaluates hosts across the clusters in "
        "scope and picks the best fitting host (most free capacity after placement), "
        "reporting its cluster. Optionally scope to a physical site via location (e.g. "
        "'lab', 'Madrid') — a site name is NOT a resource name. If nothing fits, names "
        "the blocking resource (cpu/memory) and the closest option."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "vcpu": {"type": "integer", "description": "Requested vCPU count"},
            "memory_gb": {"type": "number", "description": "Requested memory in GB"},
            "location": {"type": "string", "description": "Physical site to place within, e.g. 'lab'. Omit for the whole estate."},
            "top_n": {"type": "integer", "description": "How many host candidates to return", "default": 3},
        },
        "required": ["vcpu", "memory_gb"],
    },
    handler=_vrops_placement_recommendation,
)
