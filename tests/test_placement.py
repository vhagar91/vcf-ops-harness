"""Unit tests for VM placement (no network required)."""

from __future__ import annotations

import asyncio

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
    assert headroom_after_pct(1000.0, 5000.0, 10000.0) == -40.0


def test_headroom_after_pct_none_on_missing():
    assert headroom_after_pct(None, 8400.0, 16800.0) is None
    assert headroom_after_pct(10292.0, 8400.0, 0) is None


def test_placement_keys_present():
    assert PLACEMENT_CPU_CAPACITY_KEY in PLACEMENT_KEYS
    assert PLACEMENT_CPU_CORECOUNT_KEY in PLACEMENT_KEYS
    assert len(PLACEMENT_KEYS) >= 7


from src.actions.builtin.vrops import placement as P
from src.actions.builtin.vrops.analysis import (
    PLACEMENT_CPU_FREE_KEY,
    PLACEMENT_CPU_USAGE_PCT_KEY,
    PLACEMENT_MEM_FREE_KEY,
    PLACEMENT_MEM_USAGE_PCT_KEY,
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
    ev = P._evaluate(_stats(10292.0, 2.0), 4, 12.0)
    assert ev["fits"] is False
    assert ev["cpu"]["fits"] is True
    assert ev["memory"]["fits"] is False
    assert ev["memory"]["free_after_gb"] == -10.0
    assert ev["memory"]["raw_free_gb"] is not None


def test_evaluate_blocked_by_cpu():
    ev = P._evaluate(_stats(10292.0, 20.0), 16, 8.0)
    assert ev["fits"] is False
    assert ev["cpu"]["fits"] is False


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
            "h1": _stats(4000.0, 4.0),    # tight
            "h2": _stats(10292.0, 24.0),  # roomy: best
        },
    )
    _patch(monkeypatch, client)
    res = asyncio.run(P._vrops_placement_recommendation({"vcpu": 4, "memory_gb": 12}))
    assert res.success
    assert res.raw["recommended"]["host"] == "esx-02a"
    assert res.raw["recommended"]["cluster"] == "cluster-01a"
    assert res.raw["recommended"]["fits"] is True
    assert [c["host"] for c in res.raw["candidates"]][0] == "esx-02a"


def test_placement_crosses_clusters_to_find_fit(monkeypatch):
    # cl-A ranks first on aggregate headroom but its only host is too small for 12GB;
    # cl-B ranks lower but its host fits. The recommendation must cross to cl-B.
    client = FakeClient(
        by_kind={"ClusterComputeResource": [
            _res("clA", "cl-A", "ClusterComputeResource"),
            _res("clB", "cl-B", "ClusterComputeResource"),
        ]},
        children={"clA": [_res("ha", "esx-a", "HostSystem")],
                  "clB": [_res("hb", "esx-b", "HostSystem")]},
        stats={
            "clA": _stats(30000.0, 40.0, mem_total_gb=128.0, cpu_total=33600.0, cores=16.0),
            "ha":  _stats(10000.0, 4.0),    # only 4 GB free -> can't fit 12
            "clB": _stats(15000.0, 20.0, mem_total_gb=64.0, cpu_total=33600.0, cores=16.0),
            "hb":  _stats(10292.0, 24.0),   # fits
        },
    )
    _patch(monkeypatch, client)
    res = asyncio.run(P._vrops_placement_recommendation({"vcpu": 4, "memory_gb": 12}))
    assert res.success
    assert res.raw["recommended"]["host"] == "esx-b"
    assert res.raw["recommended"]["cluster"] == "cl-B"
    assert res.raw["recommended"]["fits"] is True


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
