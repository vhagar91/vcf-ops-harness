# vrops_diagnose Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single composite `vrops_diagnose` tool that triages a vROps resource, analyzes recent metric trends, and emits ranked recommendations as one compact structured report — so weak models (qwen3/Ollama) make one tool call and only narrate a pre-computed verdict.

**Architecture:** All analysis is deterministic Python in a pure, network-free module (`analysis.py`). A thin async handler (`diagnose.py`) orchestrates existing `VropsClient` read calls plus one new raw-series method, delegates all computation to `analysis.py`, and returns a bounded report. The system prompt routes diagnostic questions to the new tool and constrains narration.

**Tech Stack:** Python 3, async action handlers, `requests`-based `VropsClient`, pytest (network-free).

---

## File Structure

- Create `src/actions/builtin/vrops/analysis.py` — pure functions + metric catalog/thresholds. No I/O.
- Create `src/actions/builtin/vrops/diagnose.py` — `vrops_diagnose_action` `ActionDefinition` + async handler. Orchestration/I/O only.
- Modify `src/actions/builtin/vrops/vrops_client.py` — add `get_stat_series()` returning raw per-key sample lists.
- Modify `src/config/settings.py:10-33` — add routing + narration rules to `DEFAULT_SYSTEM_PROMPT`.
- Modify `src/main.py` — register `vrops_diagnose_action`.
- Create `tests/test_analysis.py` — pure-function unit tests.
- Create `tests/test_diagnose.py` — handler tests with a fake client + `get_stat_series` parse test.

Run all tests with: `.venv/bin/python -m pytest tests/ -v`

---

## Task 1: Pure analysis module — metric catalog + trend + threshold

**Files:**
- Create: `src/actions/builtin/vrops/analysis.py`
- Test: `tests/test_analysis.py`

- [ ] **Step 1: Write failing tests for trend + threshold**

Create `tests/test_analysis.py`:

```python
from src.actions.builtin.vrops.analysis import (
    compute_trend,
    evaluate_threshold,
    METRIC_CATALOG,
    STANDARD_METRIC_KEYS,
)


def test_compute_trend_rising():
    assert compute_trend([10, 12, 14, 16, 18, 20]) == "rising"


def test_compute_trend_falling():
    assert compute_trend([90, 80, 70, 60, 50]) == "falling"


def test_compute_trend_stable_flat():
    assert compute_trend([50, 50, 50, 50]) == "stable"


def test_compute_trend_stable_noisy():
    # small wobble around a constant, no real direction
    assert compute_trend([50, 51, 49, 50, 51, 49]) == "stable"


def test_compute_trend_single_point_is_stable():
    assert compute_trend([42]) == "stable"


def test_compute_trend_empty_is_stable():
    assert compute_trend([]) == "stable"


def test_evaluate_threshold_counts_breaches():
    breached, count = evaluate_threshold([10, 95, 50, 99], 90.0)
    assert breached is True
    assert count == 2


def test_evaluate_threshold_none_threshold():
    assert evaluate_threshold([100, 200], None) == (False, 0)


def test_evaluate_threshold_empty_samples():
    assert evaluate_threshold([], 90.0) == (False, 0)


def test_catalog_keys_match_standard_list():
    assert STANDARD_METRIC_KEYS == list(METRIC_CATALOG.keys())
    assert "cpu|usage_average" in METRIC_CATALOG
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_analysis.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.actions.builtin.vrops.analysis'`

- [ ] **Step 3: Create the module with catalog, `compute_trend`, `evaluate_threshold`**

Create `src/actions/builtin/vrops/analysis.py`:

```python
"""Pure, network-free analysis helpers for vrops_diagnose.

Every function here is deterministic and takes plain data, so the LLM only ever
narrates a pre-computed verdict — it cannot invent trends or numbers.
"""

from __future__ import annotations

# Default per-metric thresholds, keyed by vROps stat key. A sample breaches when
# it exceeds `threshold`. `threshold=None` means no threshold is defined.
METRIC_CATALOG: dict[str, dict] = {
    "cpu|usage_average":        {"label": "CPU %",           "threshold": 90.0, "unit": "%"},
    "mem|usage_average":        {"label": "Memory %",        "threshold": 90.0, "unit": "%"},
    "virtualDisk|totalLatency": {"label": "Disk latency",    "threshold": 20.0, "unit": "ms"},
    "net|usage_average":        {"label": "Network",         "threshold": None, "unit": "KBps"},
    "disk|usage_average":       {"label": "Disk throughput", "threshold": None, "unit": "KBps"},
}

STANDARD_METRIC_KEYS = list(METRIC_CATALOG.keys())


def compute_trend(samples: list[float], rel_tol: float = 0.05) -> str:
    """Classify a series as 'rising' | 'falling' | 'stable' via least-squares slope.

    The total modeled change across the window (slope * span) is compared against
    the series' value range; within `rel_tol` of that range counts as 'stable'.
    Fewer than 2 points -> 'stable'.
    """
    n = len(samples)
    if n < 2:
        return "stable"
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(samples) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return "stable"
    slope = sum((xs[i] - mean_x) * (samples[i] - mean_y) for i in range(n)) / denom
    total_change = slope * (n - 1)
    value_range = max(samples) - min(samples)
    threshold = max(value_range * rel_tol, 1e-9)
    if abs(total_change) < threshold:
        return "stable"
    return "rising" if total_change > 0 else "falling"


def evaluate_threshold(samples: list[float], threshold: float | None) -> tuple[bool, int]:
    """Return (breached, breach_count): how many samples exceed the threshold.

    A None threshold (no limit defined) or empty series -> (False, 0).
    """
    if threshold is None or not samples:
        return (False, 0)
    count = sum(1 for s in samples if s > threshold)
    return (count > 0, count)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_analysis.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/analysis.py tests/test_analysis.py
git commit -m "feat(vrops): pure trend + threshold analysis helpers"
```

---

## Task 2: Pure analysis — `summarize_metric`, `build_recommendations`, `rollup_verdict`

**Files:**
- Modify: `src/actions/builtin/vrops/analysis.py`
- Test: `tests/test_analysis.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_analysis.py`:

```python
from src.actions.builtin.vrops.analysis import (
    summarize_metric,
    build_recommendations,
    rollup_verdict,
)


def test_summarize_metric_basic():
    m = summarize_metric("cpu|usage_average", [80, 85, 91, 95])
    assert m["key"] == "cpu|usage_average"
    assert m["label"] == "CPU %"
    assert m["latest"] == 95.0
    assert m["avg"] == 87.75
    assert m["min"] == 80.0
    assert m["max"] == 95.0
    assert m["trend"] == "rising"
    assert m["threshold"] == 90.0
    assert m["breached"] is True
    assert m["breach_count"] == 2
    assert m["samples"] == 4


def test_summarize_metric_empty_series():
    m = summarize_metric("mem|usage_average", [])
    assert m["samples"] == 0
    assert m["latest"] is None
    assert m["trend"] == "stable"
    assert m["breached"] is False
    assert m["breach_count"] == 0


def test_summarize_metric_unknown_key_uses_fallback_meta():
    m = summarize_metric("weird|key", [1, 2, 3])
    assert m["label"] == "weird|key"
    assert m["threshold"] is None
    assert m["breached"] is False


def test_build_recommendations_critical_alert_first():
    alerts = [{"level": "CRITICAL", "name": "Host down", "alertId": "a1"}]
    recs = build_recommendations("RED", alerts, [])
    assert "Host down" in recs[0]


def test_build_recommendations_cpu_high_rising():
    metrics = [summarize_metric("cpu|usage_average", [88, 92, 95, 98])]
    recs = build_recommendations("YELLOW", [], metrics)
    assert any("CPU" in r and "climbing" in r for r in recs)


def test_build_recommendations_memory_breach():
    metrics = [summarize_metric("mem|usage_average", [95, 96, 97])]
    recs = build_recommendations("YELLOW", [], metrics)
    assert any("Memory" in r or "memory" in r for r in recs)


def test_build_recommendations_healthy_no_action():
    metrics = [summarize_metric("cpu|usage_average", [10, 12, 11])]
    recs = build_recommendations("GREEN", [], metrics)
    assert recs == ["No action needed; resource is healthy."]


def test_rollup_verdict_critical_on_red_health():
    assert rollup_verdict("RED", [], []) == "CRITICAL"


def test_rollup_verdict_critical_on_critical_alert():
    assert rollup_verdict("GREEN", [{"level": "CRITICAL"}], []) == "CRITICAL"


def test_rollup_verdict_warning_on_breach():
    metrics = [summarize_metric("cpu|usage_average", [95, 96])]
    assert rollup_verdict("GREEN", [], metrics) == "WARNING"


def test_rollup_verdict_ok_when_clean():
    metrics = [summarize_metric("cpu|usage_average", [10, 12])]
    assert rollup_verdict("GREEN", [], metrics) == "OK"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_analysis.py -v`
Expected: FAIL with `ImportError: cannot import name 'summarize_metric'`

- [ ] **Step 3: Append implementation to `analysis.py`**

Append to `src/actions/builtin/vrops/analysis.py`:

```python
def summarize_metric(key: str, samples: list[float]) -> dict:
    """Build the compact per-metric verdict for one stat key."""
    meta = METRIC_CATALOG.get(key, {"label": key, "threshold": None, "unit": ""})
    threshold = meta["threshold"]
    if not samples:
        return {
            "key": key, "label": meta["label"], "unit": meta["unit"],
            "samples": 0, "latest": None, "avg": None, "min": None, "max": None,
            "trend": "stable", "threshold": threshold,
            "breached": False, "breach_count": 0,
        }
    breached, breach_count = evaluate_threshold(samples, threshold)
    return {
        "key": key, "label": meta["label"], "unit": meta["unit"],
        "samples": len(samples),
        "latest": round(samples[-1], 2),
        "avg": round(sum(samples) / len(samples), 2),
        "min": round(min(samples), 2),
        "max": round(max(samples), 2),
        "trend": compute_trend(samples),
        "threshold": threshold,
        "breached": breached,
        "breach_count": breach_count,
    }


def build_recommendations(health_state: str | None, alerts: list[dict],
                          metrics: list[dict]) -> list[str]:
    """Map detected conditions to ranked, canned remediation suggestions."""
    recs: list[str] = []
    crit = [a for a in alerts if (a.get("level") or "").upper() in ("CRITICAL", "IMMEDIATE")]
    for a in crit:
        recs.append(f"Investigate critical alert: {a.get('name') or a.get('alertId')}.")

    by_key = {m["key"]: m for m in metrics}
    cpu = by_key.get("cpu|usage_average")
    if cpu and cpu["breached"]:
        if cpu["trend"] == "rising":
            recs.append("CPU is sustained high and climbing; investigate a runaway "
                        "process or add vCPU capacity.")
        else:
            recs.append("CPU is sustained high; review workload sizing or add vCPU capacity.")
    mem = by_key.get("mem|usage_average")
    if mem and mem["breached"]:
        recs.append("Memory usage is high; check for leaks/ballooning or add RAM.")
    disk = by_key.get("virtualDisk|totalLatency")
    if disk and disk["breached"]:
        recs.append("Disk latency is elevated; check datastore contention or the storage backend.")

    if not recs:
        if (health_state or "").upper() in ("RED", "ORANGE", "YELLOW"):
            recs.append("Health is degraded but no threshold breaches detected; "
                        "review active alerts and recent changes.")
        else:
            recs.append("No action needed; resource is healthy.")
    return recs


def rollup_verdict(health_state: str | None, alerts: list[dict],
                   metrics: list[dict]) -> str:
    """Overall verdict: OK | WARNING | CRITICAL."""
    state = (health_state or "").upper()
    levels = {(a.get("level") or "").upper() for a in alerts}
    if state == "RED" or "CRITICAL" in levels or "IMMEDIATE" in levels:
        return "CRITICAL"
    breached = any(m["breached"] for m in metrics)
    if state in ("ORANGE", "YELLOW") or breached or "WARNING" in levels:
        return "WARNING"
    return "OK"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_analysis.py -v`
Expected: PASS (22 tests total)

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/analysis.py tests/test_analysis.py
git commit -m "feat(vrops): metric summary, recommendations, verdict rollup"
```

---

## Task 3: `VropsClient.get_stat_series` — raw per-key samples

**Files:**
- Modify: `src/actions/builtin/vrops/vrops_client.py` (add method after `get_stats`, end of class ~line 970)
- Test: `tests/test_diagnose.py`

- [ ] **Step 1: Write failing parse test**

Create `tests/test_diagnose.py`:

```python
from src.actions.builtin.vrops.vrops_client import VropsClient


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _client():
    # __init__ does no network (only disables SSL warnings); we override _request.
    return VropsClient("host", "user", "pass")


def test_get_stat_series_extracts_ordered_samples():
    payload = {
        "values": [
            {"stat-list": {"stat": [
                {"statKey": {"key": "cpu|usage_average"},
                 "data": [10.0, None, 20.0, 30.0]},
            ]}}
        ]
    }
    client = _client()
    client._request = lambda method, path, **kw: _FakeResp(payload)
    series = client.get_stat_series("rid", ["cpu|usage_average"], hours_back=24)
    assert series == {"cpu|usage_average": [10.0, 20.0, 30.0]}


def test_get_stat_series_returns_empty_on_error_status():
    client = _client()
    client._request = lambda method, path, **kw: _FakeResp({}, status=500)
    assert client.get_stat_series("rid", ["cpu|usage_average"]) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_diagnose.py -v`
Expected: FAIL with `AttributeError: 'VropsClient' object has no attribute 'get_stat_series'`

- [ ] **Step 3: Add `get_stat_series` to the client**

In `src/actions/builtin/vrops/vrops_client.py`, add this method immediately after the existing `get_stats` method (at the end of the class):

```python
    def get_stat_series(self, resource_id: str, stat_keys: List[str],
                        hours_back: float = 24.0, rollup: str = "AVG",
                        interval: str = "MINUTES", interval_qty: int = 5) -> Dict[str, List[float]]:
        """Like get_stats, but returns the ordered (non-null) data points per key,
        so callers can compute trends and threshold-breach counts."""
        end = int(time.time() * 1000)
        begin = end - int(hours_back * 3600 * 1000)
        params: Dict[str, Any] = {
            "statKey": stat_keys,
            "begin": begin,
            "end": end,
            "rollUpType": rollup,
            "intervalType": interval,
            "intervalQuantifier": interval_qty,
        }
        try:
            resp = self._request("GET", f"/resources/{resource_id}/stats", params=params)
            if resp.status_code != 200:
                logging.error(f"get_stat_series failed: {resp.status_code}")
                return {}
            series: Dict[str, List[float]] = {}
            for v in resp.json().get("values", []):
                for stat in v.get("stat-list", {}).get("stat", []):
                    key = stat.get("statKey", {}).get("key")
                    data = [d for d in (stat.get("data") or []) if d is not None]
                    if key:
                        series[key] = data
            return series
        except Exception as e:
            logging.error(f"Error getting stat series for {resource_id}: {e}")
            return {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_diagnose.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/vrops_client.py tests/test_diagnose.py
git commit -m "feat(vrops): add get_stat_series for raw per-key samples"
```

---

## Task 4: `vrops_diagnose` handler + action definition

**Files:**
- Create: `src/actions/builtin/vrops/diagnose.py`
- Test: `tests/test_diagnose.py` (append)

- [ ] **Step 1: Append failing handler tests**

Append to `tests/test_diagnose.py`:

```python
import asyncio

import src.actions.builtin.vrops.diagnose as diag
from src.actions.builtin.vrops.diagnose import _vrops_diagnose, vrops_diagnose_action


class _FakeClient:
    def __init__(self, matches, health=None, alerts=None, series=None):
        self._matches = matches
        self._health = health
        self._alerts = alerts or []
        self._series = series or {}

    def search_resources(self, name, resource_kind=None, adapter_kind=None):
        return self._matches

    def get_resource_health(self, rid):
        return self._health

    def get_alerts(self, resource_id=None, active_only=True):
        return self._alerts

    def get_stat_series(self, rid, keys, hours_back=24):
        return self._series


def _run(args):
    return asyncio.run(_vrops_diagnose(args))


def _patch(monkeypatch, client):
    monkeypatch.setattr(diag, "_build_client", lambda args: client)


def test_diagnose_not_found(monkeypatch):
    _patch(monkeypatch, _FakeClient(matches=[]))
    res = _run({"name": "ghost"})
    assert res.success is False
    assert "No resource matches" in res.summary


def test_diagnose_ambiguous(monkeypatch):
    matches = [
        {"identifier": "1", "name": "web-01", "resourceKind": "VirtualMachine"},
        {"identifier": "2", "name": "web-01", "resourceKind": "HostSystem"},
    ]
    _patch(monkeypatch, _FakeClient(matches=matches))
    res = _run({"name": "web-01"})
    assert res.raw["ambiguous"] is True
    assert len(res.raw["matches"]) == 2


def test_diagnose_healthy_verdict_ok(monkeypatch):
    matches = [{"identifier": "1", "name": "web-01", "resourceKind": "VirtualMachine"}]
    client = _FakeClient(
        matches=matches,
        health={"health": "GREEN", "healthValue": 100},
        alerts=[],
        series={"cpu|usage_average": [10, 12, 11], "mem|usage_average": [40, 41, 39]},
    )
    _patch(monkeypatch, client)
    res = _run({"name": "web-01"})
    assert res.success is True
    assert res.raw["verdict"] == "OK"
    assert res.raw["resource"]["id"] == "1"
    assert res.raw["recommendations"] == ["No action needed; resource is healthy."]


def test_diagnose_critical_verdict(monkeypatch):
    matches = [{"identifier": "1", "name": "db-01", "resourceKind": "VirtualMachine"}]
    client = _FakeClient(
        matches=matches,
        health={"health": "RED", "healthValue": 10},
        alerts=[{"level": "CRITICAL", "name": "CPU exhausted", "alertId": "a1"}],
        series={"cpu|usage_average": [88, 92, 95, 98]},
    )
    _patch(monkeypatch, client)
    res = _run({"name": "db-01"})
    assert res.raw["verdict"] == "CRITICAL"
    assert "CRITICAL" in res.summary
    assert any("CPU exhausted" in r for r in res.raw["recommendations"])


def test_diagnose_partial_failure_records_gap(monkeypatch):
    matches = [{"identifier": "1", "name": "web-01", "resourceKind": "VirtualMachine"}]
    client = _FakeClient(matches=matches, health=None, alerts=[], series={})
    _patch(monkeypatch, client)
    res = _run({"name": "web-01"})
    assert "health" in res.raw["gaps"]
    assert "metrics" in res.raw["gaps"]


def test_action_definition_shape():
    assert vrops_diagnose_action.name == "vrops_diagnose"
    assert vrops_diagnose_action.input_schema["required"] == ["name"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_diagnose.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.actions.builtin.vrops.diagnose'`

- [ ] **Step 3: Create `diagnose.py`**

Create `src/actions/builtin/vrops/diagnose.py`:

```python
"""vrops_diagnose — one deterministic tool that triages a resource, analyzes its
recent metric trends, and emits ranked recommendations as a single compact report.

The model makes ONE tool call and narrates ONE structured result: no multi-step
tool chaining for weak models to derail on, and nothing to hallucinate.
"""

from __future__ import annotations

from ....config.types import ActionDefinition, ActionResult
from .actions import _build_client
from .analysis import (
    STANDARD_METRIC_KEYS,
    build_recommendations,
    rollup_verdict,
    summarize_metric,
)


async def _vrops_diagnose(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
    except Exception as e:
        return ActionResult(success=False, summary=str(e))

    name = args.get("name", "")
    resource_kind = args.get("resource_kind", "VirtualMachine")
    adapter_kind = args.get("adapter_kind")
    hours_back = args.get("hours_back", 24)

    # 1. Resolve the resource — never guess between multiple matches.
    matches = client.search_resources(name=name, resource_kind=resource_kind,
                                      adapter_kind=adapter_kind)
    if not matches:
        return ActionResult(success=False, summary=f"No resource matches '{name}'.")
    if len(matches) > 1:
        listing = ", ".join(f"{m['name']} ({m['resourceKind']})" for m in matches[:10])
        return ActionResult(
            success=True,
            summary=f"'{name}' matches {len(matches)} resources ({listing}); "
                    "ask the user which one to diagnose.",
            raw={"ambiguous": True, "matches": matches[:10]},
        )

    resource = matches[0]
    resource_id = resource["identifier"]
    report: dict = {
        "resource": {"name": resource.get("name"), "id": resource_id,
                     "kind": resource.get("resourceKind")},
        "window_hours": hours_back,
        "gaps": [],
    }

    # 2. Health (partial-failure tolerant).
    health = client.get_resource_health(resource_id)
    if health:
        report["health"] = {"state": health.get("health"), "value": health.get("healthValue")}
    else:
        report["health"] = {"state": None, "value": None}
        report["gaps"].append("health")

    # 3. Active alerts (capped).
    alerts = client.get_alerts(resource_id=resource_id, active_only=True) or []
    report["active_alerts"] = [
        {"criticality": a.get("level"), "name": a.get("name"), "alertId": a.get("alertId")}
        for a in alerts[:10]
    ]

    # 4. Metrics: raw series -> per-metric verdicts.
    series = client.get_stat_series(resource_id, STANDARD_METRIC_KEYS, hours_back=hours_back) or {}
    if not series:
        report["gaps"].append("metrics")
    metrics = [summarize_metric(k, series.get(k, [])) for k in STANDARD_METRIC_KEYS]
    report["metrics"] = metrics

    # 5. Recommendations + 6. overall verdict.
    state = report["health"]["state"]
    report["recommendations"] = build_recommendations(state, alerts, metrics)
    report["verdict"] = rollup_verdict(state, alerts, metrics)

    breaching = [m["label"] for m in metrics if m["breached"]]
    headline = f"{resource.get('name')}: {report['verdict']}"
    if breaching:
        headline += f" — breaching: {', '.join(breaching)}"
    return ActionResult(success=True, summary=headline, raw=report)


vrops_diagnose_action = ActionDefinition(
    name="vrops_diagnose",
    description=(
        "Diagnose a vROps resource in ONE call: current health, active alerts, "
        "recent metric trends (CPU, memory, disk latency, network) over a time "
        "window, and ranked remediation recommendations. Use this for questions "
        "like 'how is X doing', 'is X healthy', 'any issues with X', 'analyze X', "
        "or 'what should I do about X'. Returns a single structured report."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Resource name to diagnose"},
            "resource_kind": {
                "type": "string",
                "description": "Resource kind (e.g. VirtualMachine, HostSystem)",
                "default": "VirtualMachine",
            },
            "adapter_kind": {"type": "string", "description": "Adapter kind (e.g. VMWARE)"},
            "hours_back": {
                "type": "number",
                "description": "Trend window in hours",
                "default": 24,
            },
        },
        "required": ["name"],
    },
    handler=_vrops_diagnose,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_diagnose.py -v`
Expected: PASS (8 tests total in this file)

- [ ] **Step 5: Commit**

```bash
git add src/actions/builtin/vrops/diagnose.py tests/test_diagnose.py
git commit -m "feat(vrops): add composite vrops_diagnose action"
```

---

## Task 5: Register the action in `main.py`

**Files:**
- Modify: `src/main.py:20` (imports) and `src/main.py:53-54` (registration loop area)

- [ ] **Step 1: Add the import**

In `src/main.py`, after line 20 (`from .actions.builtin.vrops.actions import vrops_actions`), add:

```python
from .actions.builtin.vrops.diagnose import vrops_diagnose_action
```

- [ ] **Step 2: Register the action**

In `src/main.py`, immediately after the existing loop:

```python
    for action in vrops_actions:
        registry.register(action)
```

add:

```python
    registry.register(vrops_diagnose_action)
```

- [ ] **Step 3: Verify import + registration with a smoke check**

Run: `.venv/bin/python -m pytest tests/test_imports.py -v`
Expected: PASS

Then verify the module imports cleanly:
Run: `.venv/bin/python -c "from src.actions.builtin.vrops.diagnose import vrops_diagnose_action; print(vrops_diagnose_action.name)"`
Expected: prints `vrops_diagnose`

- [ ] **Step 4: Commit**

```bash
git add src/main.py
git commit -m "feat(vrops): register vrops_diagnose in main"
```

---

## Task 6: System-prompt routing + narration template

**Files:**
- Modify: `src/config/settings.py:10-33` (`DEFAULT_SYSTEM_PROMPT`)
- Test: `tests/test_diagnose.py` (append)

- [ ] **Step 1: Append a failing prompt test**

Append to `tests/test_diagnose.py`:

```python
from src.config.settings import DEFAULT_SYSTEM_PROMPT


def test_system_prompt_routes_to_diagnose():
    assert "vrops_diagnose" in DEFAULT_SYSTEM_PROMPT


def test_system_prompt_constrains_narration():
    # the narration template must forbid stating numbers not in the report
    assert "only" in DEFAULT_SYSTEM_PROMPT.lower()
    assert "report" in DEFAULT_SYSTEM_PROMPT.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_diagnose.py::test_system_prompt_routes_to_diagnose -v`
Expected: FAIL (assertion error — `vrops_diagnose` not yet in prompt)

- [ ] **Step 3: Edit `DEFAULT_SYSTEM_PROMPT`**

In `src/config/settings.py`, change the final line of the prompt from:

```python
    "- Be concise and technical. Report numbers and units exactly as returned."
)
```

to:

```python
    "- Be concise and technical. Report numbers and units exactly as returned.\n"
    "- For diagnostic questions ('how is X doing', 'is X healthy', 'any issues "
    "with X', 'analyze X', 'what should I do about X'), prefer vrops_diagnose: "
    "it returns health, alerts, metric trends, and recommendations in ONE call. "
    "Use the lower-level stat tools only for narrow, specific metric requests.\n"
    "- When narrating a vrops_diagnose report, follow this template: a one-line "
    "verdict headline, then health, then only the notable (breaching or trending) "
    "metrics, then the recommendations. State only numbers and names present in "
    "the report; never add values that are not in it."
)
```

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: PASS (all tests, including existing `test_robustness.py` and `test_imports.py`)

- [ ] **Step 5: Commit**

```bash
git add src/config/settings.py tests/test_diagnose.py
git commit -m "feat(vrops): route diagnostic questions to vrops_diagnose with narration template"
```

---

## Task 7: Final verification

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: PASS, no failures.

- [ ] **Step 2: Confirm the bot still imports end-to-end**

Run: `.venv/bin/python -c "import src.main; print('ok')"`
Expected: prints `ok` (no missing-import or syntax errors).

- [ ] **Step 3: Update CLAUDE.md Actions section (docs)**

In `CLAUDE.md`, in the `### Actions (tools)` section, after the sentence listing built-ins, add a sentence:

```markdown
The `vrops_diagnose` action is a composite tool: it resolves a resource, then runs health + alerts + trend analysis + rule-based recommendations entirely in Python (`vrops/analysis.py` holds the pure logic) and returns one compact structured report, so weak models make a single tool call and only narrate the verdict.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: describe vrops_diagnose composite action"
```

---

## Self-Review Notes (spec coverage)

- Health triage scan → Task 4 (health + alerts in report). ✔
- Historical trend analysis → Tasks 1–3 (`compute_trend`, `get_stat_series`, `summarize_metric`). ✔
- Rule-based recommendations → Task 2 (`build_recommendations`). ✔
- Single composite tool (approach B) → Task 4. ✔
- Structured analysis output + narration template → Task 6. ✔
- Partial-failure handling, disambiguation, not-found → Task 4 tests. ✔
- Network-free tests → Tasks 1–4 use pure functions + fake client. ✔
- Out of scope (anomaly detection, scheduler, sub-tools, arg-validation, env thresholds) → not built. ✔
