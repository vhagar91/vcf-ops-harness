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


# ---------------------------------------------------------------------------
# vrops_diagnose handler tests
# ---------------------------------------------------------------------------

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


def test_diagnose_ignores_canceled_alerts(monkeypatch):
    # A canceled alert no longer reflects current state: it must not appear in
    # the report and must not drive the verdict or recommendations.
    matches = [{"identifier": "1", "name": "web-01", "resourceKind": "VirtualMachine"}]
    client = _FakeClient(
        matches=matches,
        health={"health": "GREEN", "healthValue": 100},
        alerts=[{"level": "CRITICAL", "name": "Old CPU spike",
                 "alertId": "a1", "status": "CANCELED"}],
        series={"cpu|usage_average": [10, 12, 11]},
    )
    _patch(monkeypatch, client)
    res = _run({"name": "web-01"})
    assert res.raw["active_alerts"] == []
    assert res.raw["verdict"] == "OK"
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


from src.config.settings import DEFAULT_SYSTEM_PROMPT


def test_system_prompt_routes_to_diagnose():
    assert "vrops_diagnose" in DEFAULT_SYSTEM_PROMPT


def test_system_prompt_constrains_narration():
    # the narration template must forbid stating numbers not in the report
    assert "only" in DEFAULT_SYSTEM_PROMPT.lower()
    assert "report" in DEFAULT_SYSTEM_PROMPT.lower()
