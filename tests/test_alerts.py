"""Tests for the vrops_get_alerts action handler (compact summary output)."""

import asyncio
import json

import src.actions.builtin.vrops.actions as actions
from src.ai.llm import _format_tool_result, MAX_TOOL_RESULT_CHARS


class _FakeClient:
    def __init__(self, alerts):
        self._alerts = alerts

    def get_alerts(self, resource_id=None, criticality=None, active_only=True):
        return self._alerts


def _run(args):
    return asyncio.run(actions._vrops_get_alerts(args))


def _patch(monkeypatch, alerts):
    monkeypatch.setattr(actions, "_build_client", lambda args: _FakeClient(alerts))


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
