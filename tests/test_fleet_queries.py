"""Unit tests for the fleet-query feature (no network required)."""

from __future__ import annotations

import json

from src.actions.builtin.vrops.analysis import (
    free_capacity_score,
    oversize_score,
    reclaimable_vcpu,
    reclaimable_mem_gb,
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


def test_reclaimable_vcpu_converts_mhz_to_vcpus():
    # 4 vCPU, current 8400 MHz, recommended 4200 MHz -> half reclaimable -> 2.0
    assert reclaimable_vcpu(4, 8400.0, 4200.0) == 2.0


def test_reclaimable_vcpu_zero_when_not_oversized():
    assert reclaimable_vcpu(4, 8400.0, 8400.0) == 0.0
    assert reclaimable_vcpu(4, 8400.0, 9000.0) == 0.0


def test_reclaimable_vcpu_zero_on_missing_inputs():
    assert reclaimable_vcpu(None, 8400.0, 4200.0) == 0.0
    assert reclaimable_vcpu(4, None, 4200.0) == 0.0
    assert reclaimable_vcpu(4, 8400.0, None) == 0.0


def test_reclaimable_vcpu_zero_when_recommended_is_zero():
    # A 0 MHz recommendation is no signal, not "reclaim all vCPUs".
    assert reclaimable_vcpu(4, 8400.0, 0.0) == 0.0


def test_reclaimable_mem_gb_difference_in_gb():
    # 8 GiB provisioned (8388608 KB), 4 GiB recommended -> 4.0 GB reclaimable
    assert reclaimable_mem_gb(8388608.0, 4194304.0) == 4.0


def test_reclaimable_mem_gb_zero_when_not_oversized_or_missing():
    assert reclaimable_mem_gb(4194304.0, 4194304.0) == 0.0
    assert reclaimable_mem_gb(4194304.0, 8388608.0) == 0.0
    assert reclaimable_mem_gb(None, 4194304.0) == 0.0
    assert reclaimable_mem_gb(8388608.0, None) == 0.0


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


def test_sitemap_from_file_non_dict_json_is_empty(tmp_path):
    p = tmp_path / "sites.json"
    p.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert SiteMap.from_file(str(p)).known_locations() == []


def test_sitemap_from_file_malformed_json_is_empty(tmp_path):
    p = tmp_path / "sites.json"
    p.write_text("{ this is not valid json", encoding="utf-8")
    assert SiteMap.from_file(str(p)).known_locations() == []


import pytest

from src.actions.builtin.vrops.fleet import (
    resolve_scope,
    attach_stats,
    build_rows,
    collect_descendants,
    UnknownLocation,
)


class FakeClient:
    """Stub implementing only what fleet.py calls, with call recording.

    tree: {parent_id: [child resource dicts]} models the CHILD hierarchy.
    """

    def __init__(self, tree=None, datacenters=None, by_kind=None, stats=None):
        self._tree = tree or {}
        self._datacenters = datacenters or {}   # dc_name -> [{"identifier": ...}]
        self._by_kind = by_kind or {}           # kind -> [resource dicts]
        self._stats = stats or {}               # id -> {stat_key: value}
        self.stats_requested_ids = None
        self.child_calls = []

    def list_resources_by_kind(self, resource_kind, adapter_kind="VMWARE"):
        return self._by_kind.get(resource_kind, [])

    def search_resources(self, name, resource_kind=None, adapter_kind=None):
        return self._datacenters.get(name, [])

    def get_child_resources(self, resource_id, resource_kind=None, page_size=1000):
        self.child_calls.append(resource_id)
        kids = self._tree.get(resource_id, [])
        if resource_kind:
            kids = [k for k in kids if k.get("resourceKind") == resource_kind]
        return kids

    def get_latest_stats_bulk(self, resource_ids, stat_keys, chunk_size=100):
        self.stats_requested_ids = list(resource_ids)
        return {i: self._stats.get(i, {}) for i in resource_ids}


def _r(rid, name, kind):
    return {"identifier": rid, "name": name, "resourceKind": kind, "health": "GREEN"}


def test_resolve_scope_no_location_enumerates_whole_kind():
    c = FakeClient(by_kind={"ClusterComputeResource": [_r("c1", "A", "ClusterComputeResource")]})
    out = resolve_scope(c, SiteMap({}), None, "ClusterComputeResource")
    assert [r["identifier"] for r in out] == ["c1"]


def test_resolve_scope_unknown_location_raises():
    with pytest.raises(UnknownLocation) as e:
        resolve_scope(FakeClient(), SiteMap({"Madrid": ["dc-mad"]}), "Lisbon",
                      "ClusterComputeResource")
    assert e.value.location == "Lisbon"
    assert e.value.known == ["Madrid"]


def test_resolve_scope_clusters_are_direct_datacenter_children():
    c = FakeClient(
        datacenters={"dc-mad": [{"identifier": "DC1"}]},
        tree={"DC1": [_r("cl1", "MAD-CLU", "ClusterComputeResource"),
                      _r("ds1", "store", "Datastore")]},
    )
    out = resolve_scope(c, SiteMap({"Madrid": ["dc-mad"]}), "Madrid", "ClusterComputeResource")
    assert [r["name"] for r in out] == ["MAD-CLU"]
    # The cluster is the target kind, so we must NOT descend into it.
    assert "cl1" not in c.child_calls


def test_collect_descendants_walks_through_containers_to_vms():
    c = FakeClient(tree={
        "DC1": [_r("f1", "vmfolder", "VMFolder"), _r("cl1", "clu", "ClusterComputeResource")],
        "f1": [_r("vm1", "app01", "VirtualMachine")],
        "cl1": [_r("h1", "host", "HostSystem")],
        "h1": [_r("vm2", "app02", "VirtualMachine")],
    })
    out = collect_descendants(c, ["DC1"], "VirtualMachine")
    assert sorted(r["name"] for r in out) == ["app01", "app02"]


def test_collect_descendants_dedupes_across_paths():
    c = FakeClient(tree={
        "DC1": [_r("f1", "folder", "VMFolder"), _r("f2", "folder2", "VMFolder")],
        "f1": [_r("vm1", "dup", "VirtualMachine")],
        "f2": [_r("vm1", "dup", "VirtualMachine"), _r("vm3", "only", "VirtualMachine")],
    })
    out = collect_descendants(c, ["DC1"], "VirtualMachine")
    assert sorted(r["identifier"] for r in out) == ["vm1", "vm3"]


def test_collect_descendants_respects_depth_cap():
    c = FakeClient(tree={
        "DC1": [_r("a", "a", "VMFolder")],
        "a": [_r("b", "b", "VMFolder")],
        "b": [_r("vm", "leaf", "VirtualMachine")],
    })
    out = collect_descendants(c, ["DC1"], "VirtualMachine", max_depth=1)
    assert out == []


def test_attach_stats_only_fetches_in_scope_ids():
    c = FakeClient(stats={"c1": {"k": 12.0}})
    rows = attach_stats(c, [_r("c1", "A", "ClusterComputeResource")], ["k"])
    assert c.stats_requested_ids == ["c1"]
    assert rows[0]["stats"]["k"] == 12.0
    assert rows[0]["name"] == "A"


def test_build_rows_resolves_then_fetches():
    c = FakeClient(
        by_kind={"ClusterComputeResource": [_r("c1", "A", "ClusterComputeResource"),
                                            _r("c2", "B", "ClusterComputeResource")]},
        stats={"c1": {"k": 1.0}, "c2": {"k": 2.0}},
    )
    rows = build_rows(c, SiteMap({}), None, "ClusterComputeResource", ["k"])
    assert {r["name"]: r["stats"]["k"] for r in rows} == {"A": 1.0, "B": 2.0}


def test_attach_stats_skips_resources_without_id():
    c = FakeClient(stats={"c1": {"k": 1.0}})
    rows = attach_stats(c, [_r("c1", "A", "ClusterComputeResource"), {"name": "ghost"}], ["k"])
    assert [r["id"] for r in rows] == ["c1"]


# --- report tools ------------------------------------------------------------
import asyncio

from src.actions.registry import ActionRegistry
from src.actions.builtin.vrops import reports as reports_mod
from src.actions.builtin.vrops.analysis import (
    CLUSTER_CPU_REMAINING_KEY, CLUSTER_MEM_REMAINING_KEY, CLUSTER_DISK_REMAINING_KEY,
    CLUSTER_CPU_USABLE_KEY, CLUSTER_MEM_USABLE_KEY, CLUSTER_DISK_USABLE_KEY,
    VM_CURRENT_VCPU_KEY, VM_CURRENT_CPU_MHZ_KEY, VM_RECOMMENDED_CPU_MHZ_KEY,
    VM_CURRENT_MEM_KB_KEY, VM_RECOMMENDED_MEM_KB_KEY,
)


def test_report_actions_register_and_expose_to_openai():
    names = {a.name for a in reports_mod.vrops_report_actions}
    assert names == {
        "vrops_cluster_capacity_report",
        "vrops_oversized_vms_report",
        "vrops_fleet_query",
    }
    reg = ActionRegistry()
    for action in reports_mod.vrops_report_actions:
        reg.register(action)
    tool_names = {t["function"]["name"] for t in reg.to_openai_tools()}
    assert "vrops_cluster_capacity_report" in tool_names


def _cluster_stats(cpu_rem, cpu_use, mem_rem, mem_use, dsk_rem, dsk_use):
    return {
        CLUSTER_CPU_REMAINING_KEY: cpu_rem, CLUSTER_CPU_USABLE_KEY: cpu_use,
        CLUSTER_MEM_REMAINING_KEY: mem_rem, CLUSTER_MEM_USABLE_KEY: mem_use,
        CLUSTER_DISK_REMAINING_KEY: dsk_rem, CLUSTER_DISK_USABLE_KEY: dsk_use,
    }


def test_cluster_capacity_report_ranks_by_tightest_type(monkeypatch):
    c = FakeClient(
        by_kind={"ClusterComputeResource": [
            _r("c1", "TIGHT", "ClusterComputeResource"),
            _r("c2", "ROOMY", "ClusterComputeResource"),
        ]},
        stats={
            # c1: memory is the bottleneck (10% remaining); cpu 50%, storage 80%
            "c1": _cluster_stats(50.0, 100.0, 10.0, 100.0, 80.0, 100.0),
            # c2: all types comfortable
            "c2": _cluster_stats(90.0, 100.0, 95.0, 100.0, 99.0, 100.0),
        },
    )
    monkeypatch.setattr(reports_mod, "_build_client", lambda args: c)
    monkeypatch.setattr(reports_mod, "_site_map", lambda: SiteMap({}))
    res = asyncio.run(reports_mod._vrops_cluster_capacity_report({}))
    assert res.success
    clusters = res.raw["clusters"]
    assert [x["cluster"] for x in clusters] == ["TIGHT", "ROOMY"]
    leader = clusters[0]
    assert leader["bottleneck"] == "memory"
    assert leader["least_free_pct"] == 10.0
    assert leader["memory"]["capacity_remaining_pct"] == 10.0
    assert leader["cpu"]["capacity_remaining_pct"] == 50.0
    assert leader["storage"]["capacity_remaining_pct"] == 80.0


def test_oversized_vms_report_flags_and_ranks(monkeypatch):
    c = FakeClient(
        by_kind={"VirtualMachine": [
            _r("v1", "BIG", "VirtualMachine"),
            _r("v2", "RIGHTSIZED", "VirtualMachine"),
        ]},
        stats={
            "v1": {VM_CURRENT_VCPU_KEY: 4, VM_CURRENT_CPU_MHZ_KEY: 8400.0,
                   VM_RECOMMENDED_CPU_MHZ_KEY: 4200.0,
                   VM_CURRENT_MEM_KB_KEY: 8388608.0, VM_RECOMMENDED_MEM_KB_KEY: 4194304.0},
            "v2": {VM_CURRENT_VCPU_KEY: 2, VM_CURRENT_CPU_MHZ_KEY: 4200.0,
                   VM_RECOMMENDED_CPU_MHZ_KEY: 4200.0,
                   VM_CURRENT_MEM_KB_KEY: 4194304.0, VM_RECOMMENDED_MEM_KB_KEY: 4194304.0},
        },
    )
    monkeypatch.setattr(reports_mod, "_build_client", lambda args: c)
    monkeypatch.setattr(reports_mod, "_site_map", lambda: SiteMap({}))
    res = asyncio.run(reports_mod._vrops_oversized_vms_report({}))
    assert res.success
    vms = res.raw["vms"]
    assert [v["vm"] for v in vms] == ["BIG"]  # only BIG is oversized
    assert vms[0]["reclaimable_vcpu"] == 2.0
    assert vms[0]["reclaimable_mem_gb"] == 4.0


def test_oversized_report_unknown_location_errors(monkeypatch):
    monkeypatch.setattr(reports_mod, "_build_client", lambda args: FakeClient())
    monkeypatch.setattr(reports_mod, "_site_map", lambda: SiteMap({"Madrid": ["dc-mad"]}))
    res = asyncio.run(reports_mod._vrops_oversized_vms_report({"location": "Lisbon"}))
    assert res.success is False
    assert "Madrid" in res.summary and "Lisbon" in res.summary


def test_cluster_report_returns_error_on_client_failure(monkeypatch):
    class Boom(FakeClient):
        def list_resources_by_kind(self, *a, **k):
            raise RuntimeError("vROps 503")
    monkeypatch.setattr(reports_mod, "_build_client", lambda args: Boom())
    monkeypatch.setattr(reports_mod, "_site_map", lambda: SiteMap({}))
    res = asyncio.run(reports_mod._vrops_cluster_capacity_report({}))
    assert res.success is False
    assert "503" in res.summary


def test_fleet_query_rejects_sort_by_not_in_stat_keys(monkeypatch):
    monkeypatch.setattr(reports_mod, "_build_client", lambda args: FakeClient())
    monkeypatch.setattr(reports_mod, "_site_map", lambda: SiteMap({}))
    res = asyncio.run(reports_mod._vrops_fleet_query(
        {"resource_kind": "HostSystem", "stat_keys": ["a"], "sort_by": "b"}))
    assert res.success is False
    assert "sort_by" in res.summary


def test_report_actions_imported_in_main():
    import src.main as main_mod
    assert hasattr(main_mod, "vrops_report_actions")
    names = {a.name for a in main_mod.vrops_report_actions}
    assert "vrops_cluster_capacity_report" in names
