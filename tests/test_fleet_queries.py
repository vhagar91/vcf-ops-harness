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
