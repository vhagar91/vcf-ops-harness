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
