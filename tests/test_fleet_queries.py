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
