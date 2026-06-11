# Operations Assistant — Fleet-wide vROps Queries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Slack bot answer fleet-wide vROps questions ("which cluster has the fewest free resources in Madrid", "report of oversized VMs") by enumerating resources, filtering by site, bulk-fetching metrics, and returning one compact ranked report.

**Architecture:** Approach C from the spec — a shared internal fleet-query layer (`fleet.py`) powering composite report tools (`reports.py`), with site filtering via a local datacenter→location map (`sites.py`) and pure scoring helpers in `analysis.py`. Aggregation/ranking is done in Python so the model makes one tool call and only narrates, matching the existing `vrops_diagnose` philosophy.

**Tech Stack:** Python 3.13, async action handlers, `requests`-based vROps REST client, pytest (no-network unit tests with an injected fake client).

**Spec:** `docs/superpowers/specs/2026-06-11-operations-assistant-fleet-queries-design.md`

---

## File Structure

**Create:**
- `src/actions/builtin/vrops/sites.py` — `SiteMap`: load datacenter→location JSON, case-insensitive lookup.
- `src/actions/builtin/vrops/fleet.py` — enumerate → site-filter → bulk-stats pipeline; takes an injected client.
- `src/actions/builtin/vrops/reports.py` — three composite report handlers + `ActionDefinition`s.
- `tests/test_fleet_queries.py` — unit tests for the pure helpers, `SiteMap`, and the fleet pipeline (fake client, no network).

**Modify:**
- `src/actions/builtin/vrops/vrops_client.py` — add `list_resources_by_kind`, `get_descendants`, `get_latest_stats_bulk`.
- `src/actions/builtin/vrops/analysis.py` — add stat-key constants + `free_capacity_score`, `oversize_score`.
- `src/config/settings.py` — add `vrops_site_map_file` config field + load it + append a system-prompt note.
- `src/main.py` — register the three report actions.
- `.env.example` — document `VROPS_SITE_MAP_FILE`.

**Testing-decision note (house style):** the `requests`-based HTTP methods on `VropsClient` are intentionally NOT unit-tested (consistent with every existing client method); they are exercised through the fake client in `fleet.py` tests and verified manually against a live instance. TDD applies to the pure layers (`analysis.py`, `sites.py`, `fleet.py`).

---

## Task 1: Scoring helpers + stat-key constants (`analysis.py`)

**Files:**
- Modify: `src/actions/builtin/vrops/analysis.py` (append to end)
- Test: `tests/test_fleet_queries.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fleet_queries.py` with:

```python
"""Unit tests for the fleet-query feature (no network required)."""

from __future__ import annotations

import json

from src.actions.builtin.vrops.analysis import (
    free_capacity_score,
    oversize_score,
    CLUSTER_CAPACITY_KEYS,
    VM_RIGHTSIZING_KEYS,
)


# --- scoring helpers ---------------------------------------------------------
def test_free_capacity_score_is_bottleneck_minimum():
    assert free_capacity_score([10.0, 40.0, 80.0]) == 10.0


def test_free_capacity_score_ignores_missing_dimensions():
    assert free_capacity_score([None, 5.0, None]) == 5.0


def test_free_capacity_score_all_missing_returns_none():
    assert free_capacity_score([None, None]) is None
    assert free_capacity_score([]) is None


def test_oversize_score_weights_vcpu_against_memory():
    # 2 vCPU * 2 + 4 GB = 8
    assert oversize_score(2.0, 4.0) == 8.0


def test_oversize_score_treats_missing_as_zero():
    assert oversize_score(None, 3.0) == 3.0
    assert oversize_score(1.0, None) == 2.0


def test_stat_key_constants_are_nonempty_lists():
    assert isinstance(CLUSTER_CAPACITY_KEYS, list) and CLUSTER_CAPACITY_KEYS
    assert isinstance(VM_RIGHTSIZING_KEYS, list) and VM_RIGHTSIZING_KEYS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fleet_queries.py -v`
Expected: FAIL with `ImportError: cannot import name 'free_capacity_score'`.

- [ ] **Step 3: Implement the helpers**

Append to `src/actions/builtin/vrops/analysis.py`:

```python
# --- Fleet capacity / rightsizing -------------------------------------------
# Candidate vROps stat keys. Exact names vary by Aria/vROps version; confirm
# against the live instance with get_stat_keys and adjust if a key returns no
# data. (See spec "Open items".)
CLUSTER_CAPACITY_KEYS = [
    "cpu|capacityRemainingPercent",
    "mem|capacityRemainingPercent",
    "diskspace|capacityRemainingPercent",
]

VM_RIGHTSIZING_KEYS = [
    "summary|oversized",   # oversized flag (>0 means oversized)
    "cpu|reclaimable",     # reclaimable vCPUs
    "mem|reclaimable",     # reclaimable memory (GB)
]


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


def oversize_score(reclaimable_vcpu: float | None,
                   reclaimable_mem_gb: float | None) -> float:
    """Rank magnitude for an oversized VM. vCPUs and memory are not directly
    comparable, so combine them into one heuristic: each reclaimable vCPU is
    weighted as ~2 GB of memory. Missing values count as 0.
    """
    vcpu = reclaimable_vcpu or 0.0
    mem = reclaimable_mem_gb or 0.0
    return round(vcpu * 2.0 + mem, 2)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fleet_queries.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/analysis.py tests/test_fleet_queries.py
git commit -m "$(cat <<'EOF'
feat(vrops): add fleet capacity/oversize scoring helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Site map (`sites.py`)

**Files:**
- Create: `src/actions/builtin/vrops/sites.py`
- Test: `tests/test_fleet_queries.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fleet_queries.py`:

```python
from src.actions.builtin.vrops.sites import SiteMap


# --- SiteMap -----------------------------------------------------------------
def test_sitemap_datacenters_for_is_case_insensitive():
    sm = SiteMap({"Madrid": ["dc-mad-01", "dc-mad-02"]})
    assert sm.datacenters_for("madrid") == ["dc-mad-01", "dc-mad-02"]
    assert sm.datacenters_for("MADRID") == ["dc-mad-01", "dc-mad-02"]


def test_sitemap_unknown_location_returns_none():
    sm = SiteMap({"Madrid": ["dc-mad-01"]})
    assert sm.datacenters_for("Lisbon") is None


def test_sitemap_known_locations_uses_display_names():
    sm = SiteMap({"Madrid": ["dc-mad-01"], "Frankfurt": ["dc-fra-01"]})
    assert sorted(sm.known_locations()) == ["Frankfurt", "Madrid"]


def test_sitemap_from_file_missing_path_is_empty():
    assert SiteMap.from_file(None).known_locations() == []
    assert SiteMap.from_file("/nonexistent/path.json").known_locations() == []


def test_sitemap_from_file_loads_json(tmp_path):
    p = tmp_path / "sites.json"
    p.write_text(json.dumps({"Madrid": ["dc-mad-01"]}), encoding="utf-8")
    sm = SiteMap.from_file(str(p))
    assert sm.datacenters_for("Madrid") == ["dc-mad-01"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fleet_queries.py -k sitemap -v`
Expected: FAIL with `ModuleNotFoundError: No module named '...sites'`.

- [ ] **Step 3: Implement `sites.py`**

Create `src/actions/builtin/vrops/sites.py`:

```python
"""Local datacenter-name -> location mapping, loaded from a JSON config file.

The bot filters fleet queries by physical site (e.g. 'Madrid') using this map,
because the site is not modeled in vROps directly. Map shape:

    {"Madrid": ["dc-mad-01", "dc-mad-02"], "Frankfurt": ["dc-fra-01"]}

Matching is case-insensitive on the location name.
"""

from __future__ import annotations

import json
import logging
from typing import Optional


class SiteMap:
    """Immutable lookup from a physical location to its vROps Datacenter names."""

    def __init__(self, mapping: dict[str, list[str]]):
        # Preserve display names; index by lowercased location for lookups.
        self._by_location: dict[str, tuple[str, list[str]]] = {}
        for loc, dcs in (mapping or {}).items():
            self._by_location[loc.lower()] = (loc, list(dcs))

    @classmethod
    def from_file(cls, path: Optional[str]) -> "SiteMap":
        """Load a site map from a JSON file. Missing/unset path or a bad file
        yields an empty map (no site filtering available), never an exception."""
        if not path:
            return cls({})
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                logging.error("Site map %s is not a JSON object; ignoring", path)
                return cls({})
            return cls(data)
        except FileNotFoundError:
            logging.warning("Site map %s not found; no site filtering available", path)
            return cls({})
        except Exception as e:  # malformed JSON, permissions, etc.
            logging.error("Failed to load site map %s: %s", path, e)
            return cls({})

    def known_locations(self) -> list[str]:
        """Configured location display names."""
        return [display for display, _ in self._by_location.values()]

    def datacenters_for(self, location: str) -> Optional[list[str]]:
        """Datacenter names for a location, or None if the location is unknown."""
        entry = self._by_location.get((location or "").lower())
        return list(entry[1]) if entry else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fleet_queries.py -k sitemap -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/sites.py tests/test_fleet_queries.py
git commit -m "$(cat <<'EOF'
feat(vrops): add SiteMap for datacenter->location filtering

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Client enumeration + bulk-stats methods (`vrops_client.py`)

No unit test (house style: HTTP client methods are not unit-tested; they are covered by the fake client in Task 4 and verified live). This task adds the methods and confirms the module still imports.

**Files:**
- Modify: `src/actions/builtin/vrops/vrops_client.py` (add three methods inside `class VropsClient`, e.g. after `get_latest_stats`)

- [ ] **Step 1: Add `list_resources_by_kind`**

Add this method to `class VropsClient`:

```python
    def list_resources_by_kind(self, resource_kind: str, adapter_kind: str = "VMWARE",
                               page_size: int = 1000, max_resources: int = 20000) -> List[Dict[str, Any]]:
        """Enumerate every resource of a kind (no name filter), paging until done.

        search_resources requires a name; this is the whole-kind enumeration the
        fleet reports need. Capped at max_resources as a safety bound.
        """
        out: List[Dict[str, Any]] = []
        page = 0
        try:
            while len(out) < max_resources:
                params = {"resourceKind": resource_kind, "adapterKind": adapter_kind,
                          "page": page, "pageSize": page_size}
                resp = self._request("GET", "/resources", params=params)
                if resp.status_code != 200:
                    logging.error(f"list_resources_by_kind failed: {resp.status_code}")
                    break
                data = resp.json()
                batch = data.get("resourceList", [])
                for r in batch:
                    rk = r.get("resourceKey", {})
                    out.append({
                        "identifier": r.get("identifier"),
                        "name": rk.get("name"),
                        "resourceKind": rk.get("resourceKindKey"),
                        "adapterKind": rk.get("adapterKindKey"),
                        "health": r.get("resourceHealth"),
                        "healthValue": r.get("resourceHealthValue"),
                    })
                total = (data.get("pageInfo") or {}).get("totalCount")
                if not batch or total is None or len(out) >= total:
                    break
                page += 1
            return out
        except Exception as e:
            logging.error(f"Error listing resources of kind {resource_kind}: {e}")
            return out
```

- [ ] **Step 2: Add `get_descendants`**

Add this method to `class VropsClient`:

```python
    def get_descendants(self, resource_id: str, resource_kind: Optional[str] = None,
                        relationship_type: str = "DESCENDANT",
                        page_size: int = 1000) -> List[Dict[str, Any]]:
        """Related resources of a resource (default: all descendants), optionally
        filtered to one resource_kind. Used to scope a Datacenter down to its
        clusters or VMs for site filtering.
        """
        out: List[Dict[str, Any]] = []
        page = 0
        try:
            while True:
                params = {"relationshipType": relationship_type,
                          "page": page, "pageSize": page_size}
                resp = self._request("GET", f"/resources/{resource_id}/relationships",
                                     params=params)
                if resp.status_code != 200:
                    logging.error(f"get_descendants failed: {resp.status_code}")
                    break
                data = resp.json()
                batch = data.get("resourceList", [])
                for r in batch:
                    rk = r.get("resourceKey", {})
                    kind = rk.get("resourceKindKey")
                    if resource_kind and kind != resource_kind:
                        continue
                    out.append({
                        "identifier": r.get("identifier"),
                        "name": rk.get("name"),
                        "resourceKind": kind,
                        "adapterKind": rk.get("adapterKindKey"),
                        "health": r.get("resourceHealth"),
                    })
                total = (data.get("pageInfo") or {}).get("totalCount")
                if not batch or total is None or (page + 1) * page_size >= total:
                    break
                page += 1
            return out
        except Exception as e:
            logging.error(f"Error getting descendants of {resource_id}: {e}")
            return out
```

- [ ] **Step 3: Add `get_latest_stats_bulk`**

Add this method to `class VropsClient`:

```python
    def get_latest_stats_bulk(self, resource_ids: List[str], stat_keys: List[str],
                              chunk_size: int = 100) -> Dict[str, Dict[str, Any]]:
        """Latest value of each stat key for many resources, in chunked calls.

        Returns {resource_id: {stat_key: value}}. One call per chunk keeps a
        fleet report to a handful of HTTP requests instead of one-per-resource.
        """
        out: Dict[str, Dict[str, Any]] = {}
        ids = [r for r in dict.fromkeys(resource_ids) if r]
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i:i + chunk_size]
            params: Dict[str, Any] = {"resourceId": chunk}
            if stat_keys:
                params["statKey"] = stat_keys
            try:
                resp = self._request("GET", "/resources/stats/latest", params=params)
                if resp.status_code != 200:
                    logging.error(f"get_latest_stats_bulk failed: {resp.status_code}")
                    continue
                for v in resp.json().get("values", []):
                    rid = v.get("resourceId")
                    stats: Dict[str, Any] = {}
                    for stat in v.get("stat-list", {}).get("stat", []):
                        key = stat.get("statKey", {}).get("key")
                        data = stat.get("data") or []
                        if key and data:
                            stats[key] = data[-1]
                    if rid:
                        out[rid] = stats
            except Exception as e:
                logging.error(f"Error bulk-fetching stats: {e}")
        return out
```

- [ ] **Step 4: Verify the module imports cleanly**

Run: `.venv/bin/python -c "from src.actions.builtin.vrops.vrops_client import VropsClient; print([m for m in ('list_resources_by_kind','get_descendants','get_latest_stats_bulk') if hasattr(VropsClient, m)])"`
Expected: `['list_resources_by_kind', 'get_descendants', 'get_latest_stats_bulk']`

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/vrops_client.py
git commit -m "$(cat <<'EOF'
feat(vrops): client methods for fleet enumeration and bulk stats

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Fleet pipeline (`fleet.py`)

**Files:**
- Create: `src/actions/builtin/vrops/fleet.py`
- Test: `tests/test_fleet_queries.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fleet_queries.py`:

```python
import pytest

from src.actions.builtin.vrops.fleet import (
    resolve_scope,
    attach_stats,
    build_rows,
    UnknownLocation,
)


class FakeClient:
    """Stub implementing only the methods fleet.py uses, with call recording."""

    def __init__(self, by_kind=None, datacenters=None, descendants=None, stats=None):
        self._by_kind = by_kind or {}        # kind -> [resource dicts]
        self._datacenters = datacenters or {}  # dc_name -> [{"identifier": ...}]
        self._descendants = descendants or {}  # dc_id -> {kind: [resource dicts]}
        self._stats = stats or {}              # id -> {stat_key: value}
        self.stats_requested_ids = None

    def list_resources_by_kind(self, resource_kind, adapter_kind="VMWARE"):
        return self._by_kind.get(resource_kind, [])

    def search_resources(self, name, resource_kind=None, adapter_kind=None):
        return self._datacenters.get(name, [])

    def get_descendants(self, resource_id, resource_kind=None, **kwargs):
        return self._descendants.get(resource_id, {}).get(resource_kind, [])

    def get_latest_stats_bulk(self, resource_ids, stat_keys, chunk_size=100):
        self.stats_requested_ids = list(resource_ids)
        return {i: self._stats.get(i, {}) for i in resource_ids}


def _res(rid, name):
    return {"identifier": rid, "name": name, "resourceKind": "X", "health": "GREEN"}


def test_resolve_scope_no_location_enumerates_whole_kind():
    client = FakeClient(by_kind={"ClusterComputeResource": [_res("c1", "A"), _res("c2", "B")]})
    out = resolve_scope(client, SiteMap({}), None, "ClusterComputeResource")
    assert [r["identifier"] for r in out] == ["c1", "c2"]


def test_resolve_scope_unknown_location_raises():
    client = FakeClient()
    with pytest.raises(UnknownLocation) as exc:
        resolve_scope(client, SiteMap({"Madrid": ["dc-mad-01"]}), "Lisbon",
                      "ClusterComputeResource")
    assert exc.value.location == "Lisbon"
    assert exc.value.known == ["Madrid"]


def test_resolve_scope_filters_to_location_datacenters():
    client = FakeClient(
        datacenters={"dc-mad-01": [{"identifier": "DC1"}]},
        descendants={"DC1": {"ClusterComputeResource": [_res("c1", "MAD-A")]}},
    )
    out = resolve_scope(client, SiteMap({"Madrid": ["dc-mad-01"]}), "Madrid",
                        "ClusterComputeResource")
    assert [r["name"] for r in out] == ["MAD-A"]


def test_resolve_scope_dedupes_across_datacenters():
    client = FakeClient(
        datacenters={"dc-a": [{"identifier": "DCA"}], "dc-b": [{"identifier": "DCB"}]},
        descendants={
            "DCA": {"VirtualMachine": [_res("vm1", "shared")]},
            "DCB": {"VirtualMachine": [_res("vm1", "shared"), _res("vm2", "only-b")]},
        },
    )
    out = resolve_scope(client, SiteMap({"Site": ["dc-a", "dc-b"]}), "Site",
                        "VirtualMachine")
    assert sorted(r["identifier"] for r in out) == ["vm1", "vm2"]


def test_attach_stats_only_fetches_in_scope_ids():
    client = FakeClient(stats={"c1": {"cpu|capacityRemainingPercent": 12.0}})
    rows = attach_stats(client, [_res("c1", "A")], ["cpu|capacityRemainingPercent"])
    assert client.stats_requested_ids == ["c1"]
    assert rows[0]["stats"]["cpu|capacityRemainingPercent"] == 12.0
    assert rows[0]["name"] == "A"


def test_build_rows_resolves_then_fetches():
    client = FakeClient(
        by_kind={"ClusterComputeResource": [_res("c1", "A"), _res("c2", "B")]},
        stats={"c1": {"k": 1.0}, "c2": {"k": 2.0}},
    )
    rows = build_rows(client, SiteMap({}), None, "ClusterComputeResource", ["k"])
    assert {r["name"]: r["stats"]["k"] for r in rows} == {"A": 1.0, "B": 2.0}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fleet_queries.py -k "scope or attach or build_rows" -v`
Expected: FAIL with `ModuleNotFoundError: No module named '...fleet'`.

- [ ] **Step 3: Implement `fleet.py`**

Create `src/actions/builtin/vrops/fleet.py`:

```python
"""Fleet-query orchestration: enumerate -> site-filter -> bulk metrics.

Free of Slack/LLM concerns and takes an injected client, so it is unit-tested
with a fake client and no network. The client must provide:
list_resources_by_kind, search_resources, get_descendants, get_latest_stats_bulk.
"""

from __future__ import annotations

from typing import Optional

from .sites import SiteMap


class UnknownLocation(Exception):
    """Raised when a location filter is not present in the site map."""

    def __init__(self, location: str, known: list[str]):
        self.location = location
        self.known = known
        super().__init__(f"Unknown location '{location}'")


def resolve_scope(client, site_map: SiteMap, location: Optional[str],
                  resource_kind: str, adapter_kind: str = "VMWARE") -> list[dict]:
    """In-scope resources of `resource_kind`.

    No location -> the whole estate. With a location -> only resources under that
    location's configured datacenters (deduped). Raises UnknownLocation when the
    location is not configured, so callers never silently scan everything.
    """
    if not location:
        return client.list_resources_by_kind(resource_kind, adapter_kind=adapter_kind)

    dc_names = site_map.datacenters_for(location)
    if dc_names is None:
        raise UnknownLocation(location, site_map.known_locations())

    seen: set[str] = set()
    scoped: list[dict] = []
    for dc_name in dc_names:
        for dc in client.search_resources(name=dc_name, resource_kind="Datacenter"):
            dc_id = dc.get("identifier")
            if not dc_id:
                continue
            for r in client.get_descendants(dc_id, resource_kind=resource_kind):
                rid = r.get("identifier")
                if rid and rid not in seen:
                    seen.add(rid)
                    scoped.append(r)
    return scoped


def attach_stats(client, resources: list[dict], stat_keys: list[str]) -> list[dict]:
    """Bulk-fetch stat_keys for the given (already-filtered) resources and attach
    them as row['stats']. Stats are fetched ONLY for in-scope resources."""
    ids = [r["identifier"] for r in resources if r.get("identifier")]
    stats_by_id = client.get_latest_stats_bulk(ids, stat_keys)
    rows = []
    for r in resources:
        rid = r.get("identifier")
        rows.append({
            "id": rid,
            "name": r.get("name"),
            "health": r.get("health"),
            "stats": stats_by_id.get(rid, {}),
        })
    return rows


def build_rows(client, site_map: SiteMap, location: Optional[str],
               resource_kind: str, stat_keys: list[str],
               adapter_kind: str = "VMWARE") -> list[dict]:
    """Full pipeline: resolve scope (filter) THEN fetch stats."""
    resources = resolve_scope(client, site_map, location, resource_kind, adapter_kind)
    return attach_stats(client, resources, stat_keys)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fleet_queries.py -v`
Expected: PASS (all tests so far, including the 6 new fleet tests).

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/fleet.py tests/test_fleet_queries.py
git commit -m "$(cat <<'EOF'
feat(vrops): fleet pipeline (scope -> site-filter -> bulk stats)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Report tools (`reports.py`)

**Files:**
- Create: `src/actions/builtin/vrops/reports.py`
- Test: `tests/test_fleet_queries.py` (append a registration smoke test)

- [ ] **Step 1: Write the failing smoke test**

Append to `tests/test_fleet_queries.py`:

```python
from src.actions.registry import ActionRegistry


def test_report_actions_register_and_expose_to_openai():
    from src.actions.builtin.vrops.reports import vrops_report_actions
    names = {a.name for a in vrops_report_actions}
    assert names == {
        "vrops_cluster_capacity_report",
        "vrops_oversized_vms_report",
        "vrops_fleet_query",
    }
    reg = ActionRegistry()
    for action in vrops_report_actions:
        reg.register(action)
    tool_names = {t["function"]["name"] for t in reg.to_openai_tools()}
    assert "vrops_cluster_capacity_report" in tool_names
```

> Note: confirm the `to_openai_tools()` element shape (`t["function"]["name"]`) against `src/actions/registry.py`; adjust the assertion to match the registry's actual output if it differs.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fleet_queries.py -k report_actions -v`
Expected: FAIL with `ModuleNotFoundError: No module named '...reports'`.

- [ ] **Step 3: Implement `reports.py`**

Create `src/actions/builtin/vrops/reports.py`:

```python
"""Composite fleet-report tools. Each does enumerate + site-filter + aggregate +
rank in Python and returns ONE compact ranked report, so the model makes a single
call and only narrates — same philosophy as vrops_diagnose.
"""

from __future__ import annotations

from ....config.types import ActionDefinition, ActionResult
from ....config.settings import load_config
from .actions import _build_client
from .sites import SiteMap
from .fleet import build_rows, UnknownLocation
from .analysis import (
    CLUSTER_CAPACITY_KEYS,
    VM_RIGHTSIZING_KEYS,
    free_capacity_score,
    oversize_score,
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


async def _vrops_cluster_capacity_report(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
    except Exception as e:
        return ActionResult(success=False, summary=str(e))

    location = args.get("location")
    top_n = int(args.get("top_n", 5))
    sort = args.get("sort", "least_free")

    try:
        rows = build_rows(client, _site_map(), location,
                          "ClusterComputeResource", CLUSTER_CAPACITY_KEYS)
    except UnknownLocation as e:
        return _unknown_location_result(e)

    scored = []
    for r in rows:
        s = r["stats"]
        cpu = _num(s, "cpu|capacityRemainingPercent")
        mem = _num(s, "mem|capacityRemainingPercent")
        disk = _num(s, "diskspace|capacityRemainingPercent")
        score = free_capacity_score([cpu, mem, disk])
        if score is None:
            continue
        scored.append({
            "cluster": r["name"], "free_score_pct": score,
            "cpu_remaining_pct": cpu, "mem_remaining_pct": mem,
            "disk_remaining_pct": disk, "health": r["health"],
        })

    if not scored:
        scope = f" in {location}" if location else ""
        return ActionResult(success=True,
                            summary=f"No cluster capacity data available{scope}.",
                            raw={"clusters": [], "location": location})

    reverse = (sort == "most_free")
    scored.sort(key=lambda c: c["free_score_pct"], reverse=reverse)
    top = scored[:top_n]
    leader = top[0]
    descriptor = "most" if reverse else "fewest"
    headline = (f"{leader['cluster']} has the {descriptor} free capacity"
                + (f" in {location}" if location else "")
                + f" ({leader['free_score_pct']}% remaining at its tightest dimension).")
    return ActionResult(success=True, summary=headline,
                        raw={"location": location, "sort": sort,
                             "shown": len(top), "total_clusters": len(scored),
                             "clusters": top})


async def _vrops_oversized_vms_report(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
    except Exception as e:
        return ActionResult(success=False, summary=str(e))

    location = args.get("location")
    top_n = int(args.get("top_n", 20))
    min_reclaimable = args.get("min_reclaimable")

    try:
        rows = build_rows(client, _site_map(), location,
                          "VirtualMachine", VM_RIGHTSIZING_KEYS)
    except UnknownLocation as e:
        return _unknown_location_result(e)

    oversized = []
    for r in rows:
        s = r["stats"]
        flag = _num(s, "summary|oversized")
        rec_vcpu = _num(s, "cpu|reclaimable")
        rec_mem = _num(s, "mem|reclaimable")
        score = oversize_score(rec_vcpu, rec_mem)
        is_oversized = (flag is not None and flag > 0) or score > 0
        if not is_oversized:
            continue
        if min_reclaimable is not None and score < float(min_reclaimable):
            continue
        oversized.append({"vm": r["name"], "oversize_score": score,
                          "reclaimable_vcpu": rec_vcpu, "reclaimable_mem_gb": rec_mem})

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


async def _vrops_fleet_query(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
    except Exception as e:
        return ActionResult(success=False, summary=str(e))

    resource_kind = args.get("resource_kind")
    if not resource_kind:
        return ActionResult(success=False, summary="resource_kind is required.")
    location = args.get("location")
    stat_keys = args.get("stat_keys") or []
    sort_by = args.get("sort_by")
    top_n = int(args.get("top_n", 10))
    descending = bool(args.get("descending", True))

    try:
        rows = build_rows(client, _site_map(), location, resource_kind, stat_keys)
    except UnknownLocation as e:
        return _unknown_location_result(e)

    if sort_by:
        rows = [r for r in rows if isinstance(r["stats"].get(sort_by), (int, float))]
        rows.sort(key=lambda r: r["stats"][sort_by], reverse=descending)
    top = rows[:top_n]
    return ActionResult(success=True,
                        summary=(f"{len(rows)} {resource_kind} resource(s)"
                                 + (f" in {location}" if location else "")
                                 + f"; showing {len(top)}."),
                        raw={"resource_kind": resource_kind, "location": location,
                             "sort_by": sort_by, "shown": len(top),
                             "total": len(rows), "rows": top})


vrops_report_actions: list[ActionDefinition] = [
    ActionDefinition(
        name="vrops_cluster_capacity_report",
        description=(
            "Rank clusters by free capacity across a site or the whole estate in "
            "ONE call. Use for 'which cluster has the most/least free resources', "
            "'capacity report', or 'where can I place new workloads'. Optionally "
            "filter by physical location (e.g. 'Madrid'). Returns a ranked report; "
            "narrate the leader and the notable rows."
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
            "'rightsizing report', or 'where can I reclaim CPU/RAM'. Optionally filter "
            "by physical location. Returns a ranked report with reclaimable vCPU/memory."
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
            "covered by the capacity or oversized-VM reports. Enumerate a resource "
            "kind (optionally filtered by location), fetch the given stat keys, and "
            "rank by one of them. Prefer the dedicated reports when they fit."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "resource_kind": {"type": "string", "description": "e.g. HostSystem, Datastore, VirtualMachine, ClusterComputeResource"},
                "location": {"type": "string", "description": "Physical site to filter by. Omit for all sites."},
                "stat_keys": {"type": "array", "items": {"type": "string"}, "description": "Stat keys to fetch, e.g. ['cpu|usage_average']"},
                "sort_by": {"type": "string", "description": "Stat key to rank by (must be one of stat_keys)"},
                "descending": {"type": "boolean", "description": "Highest first", "default": True},
                "top_n": {"type": "integer", "description": "How many rows to return", "default": 10},
            },
            "required": ["resource_kind"],
        },
        handler=_vrops_fleet_query,
    ),
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fleet_queries.py -k report_actions -v`
Expected: PASS. If it fails on the `to_openai_tools()` shape, inspect `src/actions/registry.py` and correct the assertion (the implementation is fine).

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/reports.py tests/test_fleet_queries.py
git commit -m "$(cat <<'EOF'
feat(vrops): cluster-capacity, oversized-VM, and generic fleet report tools

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Config field, system prompt, `.env.example`

**Files:**
- Modify: `src/config/settings.py` (HarnessConfig field + load_config + DEFAULT_SYSTEM_PROMPT)
- Modify: `.env.example`

- [ ] **Step 1: Add the config field**

In `src/config/settings.py`, in `class HarnessConfig`, immediately after the `vrops_auth_source` field (currently `vrops_auth_source: str = "Local"`), add:

```python
    vrops_site_map_file: str = ""  # path to JSON mapping location -> [datacenter names]
```

- [ ] **Step 2: Load it in `load_config`**

In `load_config()`, immediately after the `vrops_auth_source=os.environ.get("VROPS_AUTH_SOURCE", "Local"),` line, add:

```python
        vrops_site_map_file=os.environ.get("VROPS_SITE_MAP_FILE", ""),
```

- [ ] **Step 3: Add a system-prompt note**

In `DEFAULT_SYSTEM_PROMPT`, replace the final string segment (the block that currently ends with `...never add values that are not in it."`) by appending a new bullet before the closing quote. Change:

```python
    "verdict headline, then health, then only the notable (breaching or trending) "
    "metrics, then the recommendations. State only numbers and names present in "
    "the report; never add values that are not in it."
)
```

to:

```python
    "verdict headline, then health, then only the notable (breaching or trending) "
    "metrics, then the recommendations. State only numbers and names present in "
    "the report; never add values that are not in it.\n"
    "- For FLEET / ranking questions across many resources ('which cluster has the "
    "most/least free capacity', 'oversized VMs', 'rightsizing report', optionally "
    "scoped to a site like 'Madrid'), use vrops_cluster_capacity_report or "
    "vrops_oversized_vms_report (or vrops_fleet_query for ad-hoc metric rankings). "
    "These return one ranked report — do NOT enumerate resources one by one. If a "
    "report says the location is unknown, tell the user the known sites it lists."
)
```

- [ ] **Step 4: Update `.env.example`**

Add (near the other `VROPS_*` entries in `.env.example`):

```bash
# Optional: path to a JSON file mapping physical sites to vROps Datacenter names,
# used to scope fleet reports by location. Example contents:
#   {"Madrid": ["dc-mad-01", "dc-mad-02"], "Frankfurt": ["dc-fra-01"]}
VROPS_SITE_MAP_FILE=
```

- [ ] **Step 5: Verify config loads**

Run: `.venv/bin/python -c "import os; os.environ.setdefault('SLACK_BOT_TOKEN','x'); os.environ.setdefault('SLACK_SIGNING_SECRET','y'); from src.config.settings import load_config; c=load_config(); print('site_map_file=', repr(c.vrops_site_map_file)); assert 'fleet' in c.system_prompt.lower() or 'FLEET' in c.system_prompt"`
Expected: prints `site_map_file= ''` and no assertion error.

- [ ] **Step 6: Commit**

```bash
git add src/config/settings.py .env.example
git commit -m "$(cat <<'EOF'
feat(config): VROPS_SITE_MAP_FILE + system prompt note for fleet reports

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Register the report tools (`main.py`)

**Files:**
- Modify: `src/main.py`
- Test: `tests/test_fleet_queries.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fleet_queries.py`:

```python
def test_report_actions_imported_in_main():
    import src.main as main_mod
    assert hasattr(main_mod, "vrops_report_actions")
    names = {a.name for a in main_mod.vrops_report_actions}
    assert "vrops_cluster_capacity_report" in names
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fleet_queries.py -k imported_in_main -v`
Expected: FAIL with `AttributeError: module 'src.main' has no attribute 'vrops_report_actions'`.

- [ ] **Step 3: Wire it in `main.py`**

Find the existing vROps import in `src/main.py` (it imports `vrops_actions` and `vrops_diagnose_action`). Add an import of `vrops_report_actions` from `src.actions.builtin.vrops.reports` alongside it (match the existing import style/path used for `vrops_diagnose_action`).

Then, in the registration block, after `registry.register(vrops_diagnose_action)`, add:

```python
    # Register vROps fleet report actions
    for action in vrops_report_actions:
        registry.register(action)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fleet_queries.py -k imported_in_main -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/main.py tests/test_fleet_queries.py
git commit -m "$(cat <<'EOF'
feat(vrops): register fleet report tools in main

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Full verification

- [ ] **Step 1: Run the whole test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all tests pass (existing `test_robustness.py`, `test_imports.py`, and the new `test_fleet_queries.py`).

- [ ] **Step 2: Import smoke check of the running app entrypoint**

Run: `.venv/bin/python -c "import src.main; print('main imports OK')"`
Expected: `main imports OK` (no import errors from the new modules/registrations).

- [ ] **Step 3: Confirm the registry exposes the new tools end-to-end**

Run:
```bash
.venv/bin/python -c "
from src.actions.registry import ActionRegistry
from src.actions.builtin.vrops.actions import vrops_actions
from src.actions.builtin.vrops.diagnose import vrops_diagnose_action
from src.actions.builtin.vrops.reports import vrops_report_actions
reg = ActionRegistry()
for a in vrops_actions + [vrops_diagnose_action] + vrops_report_actions:
    reg.register(a)
tools = [t['function']['name'] for t in reg.to_openai_tools()]
for n in ('vrops_cluster_capacity_report','vrops_oversized_vms_report','vrops_fleet_query'):
    assert n in tools, n
print('all fleet tools exposed:', len(tools), 'tools total')
"
```
Expected: prints the tool count and no assertion error. (Adjust `t['function']['name']` if Task 5 found the registry uses a different shape.)

- [ ] **Step 4: Live manual verification (requires populated `VROPS_*` + a `VROPS_SITE_MAP_FILE`)**

This step needs a live vROps instance and cannot be unit-tested. Confirm the candidate stat keys actually return data (spec "Open items"); if a key is empty, use `vrops_get_stat_keys` against a sample cluster/VM and update `CLUSTER_CAPACITY_KEYS` / `VM_RIGHTSIZING_KEYS` in `analysis.py`. Then, with the bot running, ask in Slack:
  - "¿Cuál es el clúster con menos recursos libres en Madrid?" → expect a ranked cluster report.
  - "Genera un reporte de VMs sobredimensionadas" → expect a ranked oversized-VM report.
  - Ask about an unconfigured location → expect a "known sites: …" reply.

- [ ] **Step 5: Final branch state**

The work was committed task-by-task on `feat/ops-assistant-fleet-queries`. Use the finishing-a-development-branch skill to decide on merge/PR.

---

## Self-Review

**Spec coverage:**
- Client `list_resources_by_kind` + bulk stats → Task 3. ✓
- Fleet layer (enumerate → site-filter-before-stats → bulk-fetch → aggregate → cap) → Task 4 (`build_rows`, filter-before-fetch test). ✓
- `sites.py` env-var JSON map, case-insensitive, unknown-location explicit → Task 2 + `UnknownLocation` handling in Task 5. ✓
- `vrops_cluster_capacity_report`, `vrops_oversized_vms_report`, `vrops_fleet_query` → Task 5. ✓ (native rightsizing keys in `analysis.py`, Task 1.)
- Error handling (creds, unknown location, no-data) → Task 5 handlers. ✓
- Guardrails / top-n capping → handlers cap with `[:top_n]`; outputs go through existing `_format_tool_result`. ✓
- System prompt note → Task 6. ✓
- Testing (pure analysis, sites, fleet with fake client) → Tasks 1,2,4; registration smoke → Tasks 5,7. ✓
- Out-of-scope items respected (no new transport, native rightsizing only, local map only). ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code. The two "confirm against the live instance" notes (stat keys, `to_openai_tools()` shape) are explicit verification steps, not placeholders — the code is complete and runnable as written.

**Type consistency:** `build_rows`/`attach_stats` return rows with keys `id`, `name`, `health`, `stats`; report handlers read `r["name"]`, `r["stats"]`, `r["health"]` — consistent. `free_capacity_score(list)` and `oversize_score(vcpu, mem)` signatures match their call sites. `UnknownLocation(location, known)` attributes match the handler/`test` usage. `vrops_report_actions` name is consistent across Tasks 5, 7, and the smoke tests.
