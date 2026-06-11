# VM Placement Recommendation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `vrops_placement_recommendation` — given a requested VM size (vCPU + memory GB), recommend the best cluster and host to place it on (optionally scoped to a site), in one tool call.

**Architecture:** A new `placement.py` action that reuses the existing fleet layer (`resolve_scope`, `collect_descendants`, `attach_stats`, `build_rows`) to scope clusters → pick the best fitting cluster → rank hosts beneath it. Fit is decided on the vROps capacity-engine view (post-HA/buffer); raw headroom is reported alongside. Pure fit math lives in `analysis.py`.

**Tech Stack:** Python 3.13, async action handlers, pytest (no-network: pure fit math + handler via monkeypatched fake client + `asyncio.run`).

**Spec:** `docs/superpowers/specs/2026-06-11-vm-placement-recommendation-design.md`

**Verified live:** both `ClusterComputeResource` and `HostSystem` expose `cpu|capacity_provisioned`, `cpu|corecount_provisioned`, `OnlineCapacityAnalytics|cpu|demand|capacityRemaining`, `cpu|capacity_usagepct_average`, `OnlineCapacityAnalytics|mem|demand|capacityRemaining`, `mem|usage_average`. Memory total: `mem|host_provisioned` (present on both here) with `mem|demand|usableCapacity` as fallback. MHz-per-vCPU ≈ 2100 (16800/8 host, 33600/16 cluster).

---

## File Structure

**Create:**
- `src/actions/builtin/vrops/placement.py` — `_evaluate` (pure candidate fit), `_vrops_placement_recommendation` handler, `vrops_placement_action` ActionDefinition.
- `tests/test_placement.py` — pure helper tests, `_evaluate` tests, handler tests (fake client).

**Modify:**
- `src/actions/builtin/vrops/analysis.py` — add `PLACEMENT_*` stat-key constants + `mhz_per_vcpu`, `headroom_after_pct`.
- `src/main.py` — register `vrops_placement_action`.
- `src/config/settings.py` — system-prompt note routing placement questions.

**Reuses (no change):** `fleet.py` (`resolve_scope`, `collect_descendants`, `attach_stats`, `build_rows`, `UnknownLocation`), `reports.py` (`_site_map`, `_num`, `_unknown_location_result`), `analysis.free_capacity_score`.

---

## Task 1: Placement fit helpers (`analysis.py`)

**Files:**
- Modify: `src/actions/builtin/vrops/analysis.py` (append at end)
- Test: `tests/test_placement.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_placement.py`:

```python
"""Unit tests for VM placement (no network required)."""

from __future__ import annotations

from src.actions.builtin.vrops.analysis import (
    mhz_per_vcpu,
    headroom_after_pct,
    PLACEMENT_KEYS,
    PLACEMENT_CPU_CAPACITY_KEY,
    PLACEMENT_CPU_CORECOUNT_KEY,
)


def test_mhz_per_vcpu_divides_capacity_by_cores():
    assert mhz_per_vcpu(16800.0, 8.0) == 2100.0


def test_mhz_per_vcpu_none_on_missing_or_zero():
    assert mhz_per_vcpu(None, 8.0) is None
    assert mhz_per_vcpu(16800.0, 0) is None
    assert mhz_per_vcpu(16800.0, None) is None


def test_headroom_after_pct_basic():
    # (10292 - 8400) / 16800 * 100 = 11.3
    assert headroom_after_pct(10292.0, 8400.0, 16800.0) == 11.3


def test_headroom_after_pct_can_go_negative():
    # overcommit: required exceeds free
    assert headroom_after_pct(1000.0, 5000.0, 10000.0) == -40.0


def test_headroom_after_pct_none_on_missing():
    assert headroom_after_pct(None, 8400.0, 16800.0) is None
    assert headroom_after_pct(10292.0, 8400.0, 0) is None


def test_placement_keys_present():
    assert PLACEMENT_CPU_CAPACITY_KEY in PLACEMENT_KEYS
    assert PLACEMENT_CPU_CORECOUNT_KEY in PLACEMENT_KEYS
    assert len(PLACEMENT_KEYS) >= 7
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_placement.py -v`
Expected: FAIL with `ImportError: cannot import name 'mhz_per_vcpu'`.

- [ ] **Step 3: Implement**

Append to `src/actions/builtin/vrops/analysis.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_placement.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/analysis.py tests/test_placement.py
git commit -m "$(cat <<'EOF'
feat(vrops): placement fit helpers (mhz_per_vcpu, headroom_after_pct)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Candidate evaluation (`placement.py` `_evaluate`)

**Files:**
- Create: `src/actions/builtin/vrops/placement.py`
- Test: `tests/test_placement.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_placement.py`:

```python
from src.actions.builtin.vrops import placement as P
from src.actions.builtin.vrops.analysis import (
    PLACEMENT_CPU_FREE_KEY, PLACEMENT_CPU_USAGE_PCT_KEY,
    PLACEMENT_MEM_FREE_KEY, PLACEMENT_MEM_USAGE_PCT_KEY,
    PLACEMENT_MEM_TOTAL_HOST_KEY,
)

_KB = 1024 * 1024


def _stats(cpu_free, mem_free_gb, mem_total_gb=32.0, cpu_total=16800.0,
           cores=8.0, cpu_usage=20.0, mem_usage=40.0):
    return {
        PLACEMENT_CPU_CAPACITY_KEY: cpu_total,
        PLACEMENT_CPU_CORECOUNT_KEY: cores,
        PLACEMENT_CPU_FREE_KEY: cpu_free,
        PLACEMENT_CPU_USAGE_PCT_KEY: cpu_usage,
        PLACEMENT_MEM_FREE_KEY: mem_free_gb * _KB,
        PLACEMENT_MEM_USAGE_PCT_KEY: mem_usage,
        PLACEMENT_MEM_TOTAL_HOST_KEY: mem_total_gb * _KB,
    }


def test_evaluate_fits_when_cpu_and_mem_have_room():
    # 4 vCPU -> 4*2100 = 8400 MHz; cpu free 10292 ok; mem free 20GB >= 12
    ev = P._evaluate(_stats(10292.0, 20.0), 4, 12.0)
    assert ev["fits"] is True
    assert ev["cpu"]["fits"] is True
    assert ev["cpu"]["required_mhz"] == 8400.0
    assert ev["cpu"]["free_after_mhz"] == 1892.0
    assert ev["memory"]["fits"] is True
    assert ev["memory"]["free_after_gb"] == 8.0
    assert ev["headroom_after_pct"] is not None


def test_evaluate_blocked_by_memory():
    # mem free only 2GB, request 12 -> memory blocks; cpu still fits
    ev = P._evaluate(_stats(10292.0, 2.0), 4, 12.0)
    assert ev["fits"] is False
    assert ev["cpu"]["fits"] is True
    assert ev["memory"]["fits"] is False
    assert ev["memory"]["free_after_gb"] == -10.0
    assert ev["memory"]["raw_free_gb"] is not None  # raw shown alongside


def test_evaluate_blocked_by_cpu():
    # request 16 vCPU -> 33600 MHz; cpu free only 10292 -> cpu blocks
    ev = P._evaluate(_stats(10292.0, 20.0), 16, 8.0)
    assert ev["fits"] is False
    assert ev["cpu"]["fits"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_placement.py -k evaluate -v`
Expected: FAIL with `ModuleNotFoundError: No module named '...placement'`.

- [ ] **Step 3: Implement `placement.py` (helpers + `_evaluate` only)**

Create `src/actions/builtin/vrops/placement.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_placement.py -k evaluate -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/placement.py tests/test_placement.py
git commit -m "$(cat <<'EOF'
feat(vrops): placement candidate evaluation (_evaluate)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Placement handler + ActionDefinition (`placement.py`)

**Files:**
- Modify: `src/actions/builtin/vrops/placement.py` (append handler + ActionDefinition)
- Test: `tests/test_placement.py` (append handler tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_placement.py`:

```python
import asyncio


class FakeClient:
    """Stub implementing what the fleet layer uses for placement."""

    def __init__(self, by_kind=None, children=None, stats=None):
        self._by_kind = by_kind or {}        # kind -> [resource dicts]
        self._children = children or {}      # parent_id -> [resource dicts]
        self._stats = stats or {}            # id -> stats dict

    def list_resources_by_kind(self, resource_kind, adapter_kind="VMWARE"):
        return self._by_kind.get(resource_kind, [])

    def search_resources(self, name, resource_kind=None, adapter_kind=None):
        return []

    def get_child_resources(self, resource_id, resource_kind=None, page_size=1000):
        kids = self._children.get(resource_id, [])
        if resource_kind:
            kids = [k for k in kids if k.get("resourceKind") == resource_kind]
        return kids

    def get_latest_stats_bulk(self, resource_ids, stat_keys, chunk_size=100):
        return {i: self._stats.get(i, {}) for i in resource_ids}


def _res(rid, name, kind):
    return {"identifier": rid, "name": name, "resourceKind": kind, "health": "GREEN"}


def _patch(monkeypatch, client):
    from src.actions.builtin.vrops.sites import SiteMap
    monkeypatch.setattr(P, "_build_client", lambda args: client)
    monkeypatch.setattr(P, "_site_map", lambda: SiteMap({}))


def test_placement_recommends_fitting_host(monkeypatch):
    client = FakeClient(
        by_kind={"ClusterComputeResource": [_res("cl1", "cluster-01a", "ClusterComputeResource")]},
        children={"cl1": [_res("h1", "esx-01a", "HostSystem"), _res("h2", "esx-02a", "HostSystem")]},
        stats={
            "cl1": _stats(20000.0, 40.0, mem_total_gb=64.0, cpu_total=33600.0, cores=16.0),
            "h1": _stats(4000.0, 4.0),    # tight: little room
            "h2": _stats(10292.0, 24.0),  # roomy: best fit
        },
    )
    _patch(monkeypatch, client)
    res = asyncio.run(P._vrops_placement_recommendation({"vcpu": 4, "memory_gb": 12}))
    assert res.success
    assert res.raw["recommended"]["host"] == "esx-02a"
    assert res.raw["recommended"]["cluster"] == "cluster-01a"
    assert res.raw["recommended"]["fits"] is True
    # h2 ranked above h1 (more headroom after placement)
    assert [c["host"] for c in res.raw["candidates"]][0] == "esx-02a"


def test_placement_reports_when_nothing_fits(monkeypatch):
    client = FakeClient(
        by_kind={"ClusterComputeResource": [_res("cl1", "cluster-01a", "ClusterComputeResource")]},
        children={"cl1": [_res("h1", "esx-01a", "HostSystem")]},
        stats={
            "cl1": _stats(20000.0, 2.0, mem_total_gb=64.0, cpu_total=33600.0, cores=16.0),
            "h1": _stats(10292.0, 2.0),  # only 2GB free, request 12 -> memory blocks
        },
    )
    _patch(monkeypatch, client)
    res = asyncio.run(P._vrops_placement_recommendation({"vcpu": 4, "memory_gb": 12}))
    assert res.success
    assert res.raw["recommended"]["host"] is None
    assert res.raw["recommended"]["fits"] is False
    assert "memory" in res.summary.lower()


def test_placement_requires_positive_size(monkeypatch):
    _patch(monkeypatch, FakeClient())
    res = asyncio.run(P._vrops_placement_recommendation({"vcpu": 0, "memory_gb": 12}))
    assert res.success is False
    res2 = asyncio.run(P._vrops_placement_recommendation({"memory_gb": 12}))
    assert res2.success is False


def test_placement_action_registered_shape():
    assert P.vrops_placement_action.name == "vrops_placement_recommendation"
    props = P.vrops_placement_action.input_schema["properties"]
    assert "vcpu" in props and "memory_gb" in props and "location" in props
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_placement.py -k "placement" -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_vrops_placement_recommendation'`.

- [ ] **Step 3: Implement the handler + ActionDefinition**

Append to `src/actions/builtin/vrops/placement.py`:

```python
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
        vcpu = int(vcpu)
        memory_gb = float(memory_gb)
        if vcpu <= 0 or memory_gb <= 0:
            return ActionResult(success=False, summary="vcpu and memory_gb must be positive.")
        location = args.get("location")
        top_n = int(args.get("top_n", 3))

        # Stage 1: clusters in scope.
        cl_rows = build_rows(client, _site_map(), location,
                             "ClusterComputeResource", PLACEMENT_KEYS)
        if not cl_rows:
            scope = f" in {location}" if location else ""
            return ActionResult(success=True,
                                summary=f"No clusters found{scope} to place the VM.",
                                raw={"location": location, "request": {"vcpu": vcpu, "memory_gb": memory_gb},
                                     "recommended": {"cluster": None, "host": None, "fits": False},
                                     "candidates": []})
        clusters = []
        for r in cl_rows:
            ev = _evaluate(r["stats"], vcpu, memory_gb)
            clusters.append({"cluster": r["name"], "id": r["id"], **ev})
        clusters.sort(key=_rank_key, reverse=True)
        best_cluster = clusters[0]

        # Stage 2: hosts beneath the chosen cluster.
        host_res = collect_descendants(client, [best_cluster["id"]], "HostSystem")
        host_rows = attach_stats(client, host_res, PLACEMENT_KEYS)
        candidates = []
        for r in host_rows:
            ev = _evaluate(r["stats"], vcpu, memory_gb)
            candidates.append({"host": r["name"], "cluster": best_cluster["cluster"], **ev})
        candidates.sort(key=_rank_key, reverse=True)
        top = candidates[:top_n]

        rec_host = top[0] if (top and top[0]["fits"]) else None
        req = {"vcpu": vcpu, "memory_gb": memory_gb}
        loc_txt = f" in {location}" if location else ""

        if rec_host is not None:
            headline = (f"Place the {vcpu} vCPU / {memory_gb:g} GB VM on "
                        f"{rec_host['host']} (cluster {best_cluster['cluster']}){loc_txt}. "
                        f"After placement, bottleneck headroom ~{rec_host['headroom_after_pct']}% "
                        f"(cpu free {rec_host['cpu']['free_after_mhz']} MHz, "
                        f"mem free {rec_host['memory']['free_after_gb']} GB).")
            recommended = {"cluster": best_cluster["cluster"], "host": rec_host["host"], "fits": True}
        else:
            closest = top[0] if top else (best_cluster if not top else None)
            if top:
                blockers = ", ".join(_blockers(top[0])) or "capacity"
                headline = (f"No host can fit the {vcpu} vCPU / {memory_gb:g} GB VM{loc_txt} "
                            f"— blocked by {blockers}. Closest: {top[0]['host']} "
                            f"(cpu free {top[0]['cpu']['free_mhz']} MHz, "
                            f"mem free {top[0]['memory']['free_gb']} GB, "
                            f"raw mem free {top[0]['memory']['raw_free_gb']} GB).")
            else:
                headline = (f"Cluster {best_cluster['cluster']} has no hosts to evaluate{loc_txt}.")
            recommended = {"cluster": best_cluster["cluster"], "host": None, "fits": False}

        return ActionResult(success=True, summary=headline,
                            raw={"request": req, "location": location,
                                 "recommended": recommended,
                                 "note": ("fit uses vROps capacity-engine free capacity "
                                          "(post-HA/buffer); raw_free_* is total minus usage."),
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
        "a new VM', 'capacity to host a VM'. Picks the best cluster for the site then "
        "the best host within it, ranked by free capacity remaining after placement. "
        "Optionally scope to a physical site via location (e.g. 'lab', 'Madrid') — a "
        "site name is NOT a resource name. If nothing fits, names the blocking resource "
        "(cpu/memory) and the closest option."
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_placement.py -v`
Expected: PASS (all placement tests).

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/placement.py tests/test_placement.py
git commit -m "$(cat <<'EOF'
feat(vrops): vrops_placement_recommendation (cluster->host VM placement)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Register tool + system prompt

**Files:**
- Modify: `src/main.py`
- Modify: `src/config/settings.py`
- Test: `tests/test_placement.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_placement.py`:

```python
def test_placement_action_imported_in_main():
    import src.main as main_mod
    assert hasattr(main_mod, "vrops_placement_action")
    assert main_mod.vrops_placement_action.name == "vrops_placement_recommendation"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_placement.py -k imported_in_main -v`
Expected: FAIL with `AttributeError: module 'src.main' has no attribute 'vrops_placement_action'`.

- [ ] **Step 3: Wire `main.py`**

In `src/main.py`, next to the existing `from .actions.builtin.vrops.reports import vrops_report_actions` import, add:

```python
from .actions.builtin.vrops.placement import vrops_placement_action
```

Then, after the `for action in vrops_report_actions: registry.register(action)` block, add:

```python
    registry.register(vrops_placement_action)
```

- [ ] **Step 4: Add the system-prompt note**

In `src/config/settings.py`, find the fleet bullet in `DEFAULT_SYSTEM_PROMPT` that ends with `"... tell the user the known sites it lists."` and append a new bullet before the closing `)`:

```python
    "\n"
    "- For PLACEMENT questions ('where should I place / put a VM of N vCPU and M GB', "
    "'best host for a new VM', optionally in a site), use vrops_placement_recommendation "
    "with vcpu, memory_gb, and (optional) location. Report the recommended host/cluster, "
    "or if nothing fits, the blocking resource (cpu/memory) and the closest option."
```

- [ ] **Step 5: Run to verify pass + no regressions**

Run: `.venv/bin/python -m pytest tests/test_placement.py -k imported_in_main -v`
Expected: PASS.
Run: `.venv/bin/python -c "import src.main; print('main OK')"`
Expected: `main OK`.

- [ ] **Step 6: Commit**

```bash
git add src/main.py src/config/settings.py tests/test_placement.py
git commit -m "$(cat <<'EOF'
feat(vrops): register placement tool + system prompt routing

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Full verification

- [ ] **Step 1: Whole suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass (existing + new `tests/test_placement.py`).

- [ ] **Step 2: Registry exposure**

Run:
```bash
.venv/bin/python -c "
from src.actions.registry import ActionRegistry
from src.actions.builtin.vrops.placement import vrops_placement_action
reg = ActionRegistry(); reg.register(vrops_placement_action)
names = [t['function']['name'] for t in reg.to_openai_tools()]
assert 'vrops_placement_recommendation' in names
print('placement tool exposed')
"
```
Expected: `placement tool exposed`.

- [ ] **Step 3: Live verification (needs populated `VROPS_*` + `VROPS_SITE_MAP_FILE`)**

With the bot able to reach vROps, evaluate the motivating case directly:
```bash
PYTHONPATH=. .venv/bin/python -c "
import os, asyncio, json
for line in open('.env'):
    line=line.strip()
    if line and not line.startswith('#') and '=' in line:
        k,v=line.split('=',1); os.environ[k.strip()]=v.strip()
os.environ['VROPS_SITE_MAP_FILE']='vrops-site-map.example.json'
from src.actions.builtin.vrops import placement as P
r=asyncio.run(P._vrops_placement_recommendation({'vcpu':4,'memory_gb':12,'location':'lab'}))
print(r.summary)
print(json.dumps(r.raw['recommended']))
"
```
Expected: a clear recommendation, or a "blocked by memory" message with the closest host (the lab is memory-constrained). Confirm CPU fit reasoning (4 vCPU ≈ 8400 MHz vs free) and memory blocker are sensible. If a stat key returns no data, re-check with `get_stat_keys` and adjust the `PLACEMENT_*` constants.

- [ ] **Step 4: Slack smoke (optional)**

Ask: "¿Dónde coloco una VM de 4 vCPU y 12 GB en lab?" → expect the model to call `vrops_placement_recommendation` (not a name search) and narrate the recommendation/blocker.

---

## Self-Review

**Spec coverage:**
- Cluster→host two-stage → Task 3 handler (Stage 1 clusters, Stage 2 hosts). ✓
- Fit basis capacity-engine + raw shown → `_evaluate` (Task 2): fit on `*_FREE_KEY`, `raw_free_*` reported. ✓
- Best = most free after placement → `headroom_after_pct` + `_rank_key` (Tasks 1, 3). ✓
- vCPU→MHz via candidate ratio → `mhz_per_vcpu` (Task 1); verified clusters+hosts both expose the keys, so no host-derived fallback needed. ✓
- Stat keys → `PLACEMENT_*` (Task 1), all verified live. ✓
- Output shape (request/recommended/candidates, per-dim breakdown) → Task 3 raw. ✓
- Nothing-fits → blocker + closest → Task 3 `_blockers` + headline. ✓
- Error handling (creds, unknown location, non-positive size, no data, never raises) → Task 3 try/except. ✓
- System prompt note → Task 4. ✓
- Tests (pure helpers, `_evaluate`, handler via fake client, registration) → Tasks 1–4. ✓
- Out-of-scope respected (no provisioning, no storage, no batch, no DRS rules). ✓

**Placeholder scan:** No TBD/TODO; every code step is complete. The live step's "adjust constants if a key returns no data" is a verification instruction, not a placeholder — the constants are concrete and verified.

**Type consistency:** `_evaluate` returns `{fits, headroom_after_pct, cpu{...}, memory{...}}`; the handler reads `c["fits"]`, `c["headroom_after_pct"]`, `c["cpu"]["fits"]`, `c["memory"]["fits"]`, `c["cpu"]["free_after_mhz"]`, `c["memory"]["free_after_gb"]/["free_gb"]/["raw_free_gb"]` — all present. `_rank_key`/`_blockers` consume the same shape. `build_rows` rows expose `id`/`name`/`stats`; handler uses `r["id"]`, `r["name"]`, `r["stats"]`. `vrops_placement_action` name consistent across Tasks 3, 4, 5 and tests.
```
