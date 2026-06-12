"""vROps alert webhook -> enriched prompt -> agentic pipeline -> publish."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Optional

from ..config.types import PipelineEvent
from ..pipeline.orchestrator import run_pipeline
from ..utils.logger import info, error
from .publisher import Publisher

# Criticality ordering for the optional floor filter (least -> most severe).
_CRIT_ORDER = ["INFORMATION", "WARNING", "IMMEDIATE", "CRITICAL"]


@dataclass
class AlertInfo:
    alert_id: Optional[str]
    name: Optional[str]
    criticality: Optional[str]
    status: Optional[str]
    resource_id: Optional[str]
    resource_name: Optional[str]
    start_time: Optional[object]
    raw: dict = field(default_factory=dict)


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def parse_alert(payload: dict) -> AlertInfo:
    """Extract alert fields from a (user-defined) vROps webhook payload, tolerantly."""
    crit = _first(payload, "criticality", "alertLevel", "alertCriticality", "status")
    return AlertInfo(
        alert_id=_first(payload, "alertId", "alert_id", "id"),
        name=_first(payload, "alertName", "alertDefinitionName", "name"),
        criticality=str(crit).upper() if crit is not None else None,
        status=_first(payload, "status", "alertStatus"),
        resource_id=_first(payload, "resourceId", "resource_id", "entityId"),
        resource_name=_first(payload, "resourceName", "resource_name", "entityName"),
        start_time=_first(payload, "startTimeUTC", "startDate", "startTime"),
        raw=payload,
    )


def passes_criticality(alert: AlertInfo, min_criticality: str) -> bool:
    """True if the alert meets the configured floor. Empty floor -> always True;
    an unknown criticality is never silently dropped."""
    if not min_criticality:
        return True
    floor = min_criticality.upper()
    try:
        return _CRIT_ORDER.index(alert.criticality or "") >= _CRIT_ORDER.index(floor)
    except ValueError:
        return True


def build_prompt(alert: AlertInfo, context: dict) -> str:
    """Build the message fed to the agentic pipeline."""
    name = alert.resource_name or context.get("resource_name") or alert.resource_id or "unknown object"
    kind = context.get("resource_kind") or ""
    detail = context.get("alert_detail")
    lines = [
        "A vROps alert fired. Respond for on-call operators with EXACTLY: "
        "1) a one-line executive summary, 2) the affected object, 3) three concrete "
        "remediation steps. Be specific and technical; never invent values.",
        "",
        f"Alert: {alert.name or '(unnamed)'} (criticality {alert.criticality or 'UNKNOWN'})",
        f"Affected object: {name} {('[' + kind + ']') if kind else ''}".strip(),
        f"Status: {alert.status or 'UNKNOWN'}  Started: {alert.start_time or 'n/a'}",
    ]
    if detail:
        lines.append(f"Alert detail: {json.dumps(detail)[:1500]}")
    lines.append(f"Raw payload: {json.dumps(alert.raw)[:1500]}")
    return "\n".join(lines)
