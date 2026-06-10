"""Tests for the vrops_get_alerts action handler (compact summary output)."""

import asyncio
import json

import src.actions.builtin.vrops.actions as actions
from src.ai.llm import _format_tool_result, MAX_TOOL_RESULT_CHARS


class _FakeClient:
    def __init__(self, alerts, names=None):
        self._alerts = alerts
        self._names = names or {}

    def get_alerts(self, resource_id=None, criticality=None, active_only=True):
        return self._alerts

    def get_resource_names(self, resource_ids):
        return self._names


def _run(args):
    return asyncio.run(actions._vrops_get_alerts(args))


def _patch(monkeypatch, alerts, names=None):
    monkeypatch.setattr(actions, "_build_client", lambda args: _FakeClient(alerts, names))


def _many_alerts(n):
    return [
        {"alertId": f"36382301-aa45-4848-be6b-43cef4cb7a{i:02d}",
         "name": "RDS Recomendation Execute", "level": "INFORMATION",
         "status": "ACTIVE", "resourceId": f"36a3b1f0-33f8-48b1-8bff-a833c49caf{i:02d}",
         "startTimeUTC": 1780683593480 + i, "impact": "RISK"}
        for i in range(n)
    ]


def test_get_alerts_reports_accurate_total_for_large_set(monkeypatch):
    _patch(monkeypatch, _many_alerts(50))
    res = _run({"active_only": True})
    assert res.success is True
    assert res.raw["total"] == 50
    assert res.raw["by_criticality"] == {"INFORMATION": 50}
    assert "50 alert(s)" in res.summary


def test_get_alerts_summary_survives_context_bounding(monkeypatch):
    # The whole point of the fix: the result must fit the budget intact, so the
    # total and breakdown are never truncated away before the model sees them.
    _patch(monkeypatch, _many_alerts(50))
    res = _run({"active_only": True})
    formatted = _format_tool_result(res)
    assert len(formatted) <= MAX_TOOL_RESULT_CHARS
    assert "truncated" not in formatted
    payload = json.loads(formatted)
    assert payload["data"]["total"] == 50


def test_get_alerts_empty(monkeypatch):
    _patch(monkeypatch, [])
    res = _run({"active_only": True})
    assert res.raw["total"] == 0
    assert res.summary == "No alerts found"


def test_get_alerts_handler_resolves_resource_names(monkeypatch):
    alerts = [{"alertId": "a1", "name": "vCenter app health is affected",
               "level": "CRITICAL", "status": "ACTIVE", "resourceId": "r1",
               "startTimeUTC": 1, "impact": "HEALTH"}]
    names = {"r1": {"name": "vCenter-vc-l-01a.corp.local", "kind": "VC_APP"}}
    _patch(monkeypatch, alerts, names)
    res = _run({"criticality": "CRITICAL"})
    top = res.raw["top"]
    assert top[0]["resourceName"] == "vCenter-vc-l-01a.corp.local"
    assert top[0]["resourceKind"] == "VC_APP"
    assert top[0]["resourceId"] == "r1"


def test_get_alerts_handler_tolerates_unresolved_name(monkeypatch):
    alerts = [{"alertId": "a1", "name": "X", "level": "CRITICAL",
               "status": "ACTIVE", "resourceId": "r1", "startTimeUTC": 1, "impact": "H"}]
    _patch(monkeypatch, alerts, names={})  # resolution returned nothing
    res = _run({"criticality": "CRITICAL"})
    assert res.raw["top"][0]["resourceName"] is None


# --- Client-level: get_alerts uses POST /alerts/query (server-side filtering) ---
from src.actions.builtin.vrops.vrops_client import VropsClient


class _QueryResp:
    def __init__(self, alerts, total, page, page_size):
        self.status_code = 200
        self._j = {"pageInfo": {"totalCount": total, "page": page, "pageSize": page_size},
                   "alerts": alerts}

    def json(self):
        return self._j


def _raw(i, status="ACTIVE", level="CRITICAL"):
    return {"alertId": f"a{i}", "alertDefinitionName": f"Def {i}", "alertLevel": level,
            "status": status, "resourceId": f"r{i}", "startTimeUTC": i, "alertImpact": "risk"}


def _query_client(dataset, captured):
    client = VropsClient("h", "u", "p")

    def fake_request(method, path, **kw):
        captured.append({"method": method, "path": path,
                         "json": kw.get("json"), "params": kw.get("params")})
        ps = kw["params"]["pageSize"]
        page = kw["params"]["page"]
        start = page * ps
        return _QueryResp(dataset[start:start + ps], total=len(dataset),
                          page=page, page_size=ps)

    client._request = fake_request
    return client


def test_get_alerts_uses_query_endpoint_with_active_only():
    captured = []
    client = _query_client([_raw(0)], captured)
    client.get_alerts(active_only=True)
    assert captured[0]["method"] == "POST"
    assert captured[0]["path"] == "/alerts/query"
    assert captured[0]["json"]["activeOnly"] is True


def test_get_alerts_paginates_query_results():
    # 5 alerts, page_size 2 -> 3 pages; all must be assembled.
    dataset = [_raw(i) for i in range(5)]
    captured = []
    client = _query_client(dataset, captured)
    out = client.get_alerts(active_only=True, page_size=2)
    assert len(out) == 5
    assert sum(1 for c in captured if c["path"] == "/alerts/query") == 3


def test_get_alerts_maps_info_criticality_to_information():
    captured = []
    client = _query_client([_raw(0, level="INFORMATION")], captured)
    client.get_alerts(criticality="INFO")
    assert captured[0]["json"]["alertCriticality"] == ["INFORMATION"]


def test_get_alerts_scopes_by_resource_id():
    captured = []
    client = _query_client([_raw(0)], captured)
    client.get_alerts(resource_id="res-123")
    assert captured[0]["json"]["resource-query"] == {"resourceId": ["res-123"]}


# --- Client-level: get_resource_names batch-resolves IDs to names ---
class _ResResp:
    def __init__(self, resource_list):
        self.status_code = 200
        self._j = {"resourceList": resource_list,
                   "pageInfo": {"totalCount": len(resource_list)}}

    def json(self):
        return self._j


def test_get_resource_names_maps_ids_to_name_and_kind():
    client = VropsClient("h", "u", "p")
    rl = [
        {"identifier": "id1", "resourceKey": {"name": "vc-01", "resourceKindKey": "VC_APP"}},
        {"identifier": "id2", "resourceKey": {"name": "nsxt", "resourceKindKey": "NSXTAdapterInstance"}},
    ]
    captured = {}

    def fake_request(method, path, **kw):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = kw.get("params")
        return _ResResp(rl)

    client._request = fake_request
    # duplicate + falsy ids must be de-duplicated/ignored
    m = client.get_resource_names(["id1", "id2", "id2", None, ""])
    assert captured["method"] == "GET"
    assert captured["path"] == "/resources"
    assert m["id1"] == {"name": "vc-01", "kind": "VC_APP"}
    assert m["id2"]["name"] == "nsxt"


def test_get_resource_names_empty_input_makes_no_request():
    client = VropsClient("h", "u", "p")

    def boom(*a, **k):
        raise AssertionError("should not call the API for an empty id list")

    client._request = boom
    assert client.get_resource_names([]) == {}
