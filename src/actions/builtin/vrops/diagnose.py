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
        listing = ", ".join(f"{m.get('name')} ({m.get('resourceKind')})" for m in matches[:10])
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

    # 3. Active alerts (capped). get_alerts filters to active alerts server-side
    # (activeOnly), so canceled alerts never reach the verdict or recommendations.
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
    headline = f"{resource.get('name') or resource_id}: {report['verdict']}"
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
                "description": "Resource kind (e.g. VirtualMachine, HostSystem,ClusterComputeResource)",
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
