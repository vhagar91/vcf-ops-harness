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
    assert headroom_after_pct(1000.0, 5000.0, 10000.0) == -40.0


def test_headroom_after_pct_none_on_missing():
    assert headroom_after_pct(None, 8400.0, 16800.0) is None
    assert headroom_after_pct(10292.0, 8400.0, 0) is None


def test_placement_keys_present():
    assert PLACEMENT_CPU_CAPACITY_KEY in PLACEMENT_KEYS
    assert PLACEMENT_CPU_CORECOUNT_KEY in PLACEMENT_KEYS
    assert len(PLACEMENT_KEYS) >= 7
