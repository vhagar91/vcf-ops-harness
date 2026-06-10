"""vROps action definitions — expose VropsClient methods as LLM-callable tools."""

from __future__ import annotations

from ....config.types import ActionDefinition, ActionResult
from .vrops_client import VropsClient
from .analysis import summarize_alerts
from ....utils.logger import info, error

# ---------------------------------------------------------------------------
# In-memory client cache (lazy auth per server)
# ---------------------------------------------------------------------------
_client_cache: dict[str, VropsClient] = {}

_CREDENTIAL_ERR = (
    "vROps credentials not configured. "
    "Set VROPS_SERVER, VROPS_USERNAME, and VROPS_PASSWORD in your .env file."
)


def _get_client(server: str, username: str, password: str, auth_source: str) -> VropsClient:
    key = f"{server}/{username}"
    if key not in _client_cache:
        info("Creating new vROps client", server=server, user=username)
        client = VropsClient(server, username, password, auth_source)
        if not client.authenticate():
            raise RuntimeError(f"vROps authentication failed for {username}@{server}")
        _client_cache[key] = client
    return _client_cache[key]


# ---------------------------------------------------------------------------
# Action handler helpers
# ---------------------------------------------------------------------------

def _build_client(args: dict) -> VropsClient:
    from ....config.settings import load_config
    config = load_config()
    if not config.vrops_server or not config.vrops_username or not config.vrops_password:
        raise RuntimeError(_CREDENTIAL_ERR)
    return _get_client(
        server=config.vrops_server,
        username=config.vrops_username,
        password=config.vrops_password,
        auth_source=config.vrops_auth_source,
    )


# ---------------------------------------------------------------------------
# Individual action handlers
# ---------------------------------------------------------------------------

async def _vrops_get_version(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        version = client.get_version()
        if version:
            return ActionResult(success=True, summary=f"vROps version: {version}")
        return ActionResult(success=False, summary="Failed to retrieve vROps version")
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_find_resource(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        resource_id = client.find_resource(
            name=args.get("name", ""),
            resource_kind=args.get("resource_kind", "VirtualMachine"),
            adapter_kind=args.get("adapter_kind", "VMWARE"),
        )
        if resource_id:
            return ActionResult(
                success=True,
                summary=f"Found resource '{args.get('name')}' with ID: {resource_id}",
                raw={"resource_id": resource_id},
            )
        return ActionResult(
            success=False,
            summary=f"Resource '{args.get('name')}' ({args.get('resource_kind')}) not found",
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_get_resource_properties(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        props = client.get_resource_properties(args.get("resource_id", ""))
        if props:
            return ActionResult(
                success=True,
                summary=f"Retrieved properties for resource {args.get('resource_id')}",
                raw=props,
            )
        return ActionResult(
            success=False,
            summary=f"Failed to get properties for resource {args.get('resource_id')}",
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_create_resource(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        resource_id = client.create_resource(
            adapter_kind=args.get("adapter_kind", ""),
            resource_kind=args.get("resource_kind", ""),
            name=args.get("name", ""),
            identifiers=args.get("identifiers", {}),
            description=args.get("description", ""),
        )
        if resource_id:
            return ActionResult(
                success=True,
                summary=f"Created resource '{args.get('name')}' with ID: {resource_id}",
                raw={"resource_id": resource_id},
            )
        return ActionResult(
            success=False,
            summary=f"Failed to create resource '{args.get('name')}'",
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_push_properties(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        resource_id = args.get("resource_id", "")
        properties = args.get("properties", {})
        ok = client.push_properties(resource_id, properties)
        if ok:
            return ActionResult(
                success=True,
                summary=f"Pushed {len(properties)} properties to resource {resource_id}",
            )
        return ActionResult(
            success=False,
            summary=f"Failed to push properties to resource {resource_id}",
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_push_event(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        ok = client.push_event(
            resource_id=args.get("resource_id", ""),
            event_type=args.get("event_type", "INFO"),
            message=args.get("message", ""),
            severity=args.get("severity", "WARNING"),
        )
        if ok:
            return ActionResult(
                success=True,
                summary=f"Pushed event to resource {args.get('resource_id')}",
            )
        return ActionResult(
            success=False,
            summary=f"Failed to push event to resource {args.get('resource_id')}",
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_get_monitored_vcenters(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        vcenters = client.get_monitored_vcenters()
        names = [v["name"] for v in vcenters]
        return ActionResult(
            success=True,
            summary=f"Discovered {len(vcenters)} vCenter(s): {', '.join(names)}",
            raw=vcenters,
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_get_monitored_nsxt(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        managers = client.get_monitored_nsxt_managers()
        names = [m["name"] for m in managers]
        return ActionResult(
            success=True,
            summary=f"Discovered {len(managers)} NSX-T manager(s): {', '.join(names)}",
            raw=managers,
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_add_child_relationship(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        parent_id = args.get("parent_id", "")
        child_id = args.get("child_id", "")
        ok = client.add_child_relationship(parent_id, child_id)
        if ok:
            return ActionResult(
                success=True,
                summary=f"Added child relationship: {parent_id} -> {child_id}",
            )
        return ActionResult(
            success=False,
            summary=f"Failed to add child relationship: {parent_id} -> {child_id}",
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


# ---------------------------------------------------------------------------
# Read actions: alerts / health / performance (what users actually ask for)
# ---------------------------------------------------------------------------

async def _vrops_search_resources(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        matches = client.search_resources(
            name=args.get("name", ""),
            resource_kind=args.get("resource_kind"),
            adapter_kind=args.get("adapter_kind"),
        )
        if not matches:
            return ActionResult(success=True, summary=f"No resources match '{args.get('name')}'", raw=[])
        names = ", ".join(f"{m['name']} ({m['resourceKind']})" for m in matches[:10])
        return ActionResult(
            success=True,
            summary=f"Found {len(matches)} resource(s): {names}",
            raw=matches,
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_get_resource_health(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        health = client.get_resource_health(args.get("resource_id", ""))
        if health:
            return ActionResult(
                success=True,
                summary=f"{health.get('name')} health: {health.get('health')} "
                        f"(value {health.get('healthValue')})",
                raw=health,
            )
        return ActionResult(success=False, summary=f"No health data for resource {args.get('resource_id')}")
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_get_alerts(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        alerts = client.get_alerts(
            resource_id=args.get("resource_id"),
            criticality=args.get("criticality"),
            active_only=args.get("active_only", True),
        )
        # Resolve resource IDs to object names so the model reports names, not
        # opaque UUIDs. One batched lookup for all alerts.
        names = client.get_resource_names([a.get("resourceId") for a in alerts])
        for a in alerts:
            info = names.get(a.get("resourceId")) or {}
            a["resourceName"] = info.get("name")
            a["resourceKind"] = info.get("kind")
        # Aggregate into a compact, complete summary. Returning the raw list would
        # be capped/truncated before the model sees it, so large alert sets could
        # be neither counted nor described (see summarize_alerts docstring).
        summary = summarize_alerts(alerts)
        if summary["total"]:
            breakdown = ", ".join(f"{lvl} {n}" for lvl, n in summary["by_criticality"].items())
            headline = f"{summary['total']} alert(s): {breakdown}"
        else:
            headline = "No alerts found"
        return ActionResult(success=True, summary=headline, raw=summary)
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_get_alert(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        alert = client.get_alert(args.get("alert_id", ""))
        if alert:
            return ActionResult(success=True, summary=f"Alert {args.get('alert_id')} detail", raw=alert)
        return ActionResult(success=False, summary=f"Alert {args.get('alert_id')} not found")
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_get_stat_keys(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        keys = client.get_stat_keys(args.get("resource_id", ""))
        return ActionResult(
            success=True,
            summary=f"{len(keys)} metric key(s) available",
            raw=keys,
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_get_latest_stats(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        stats = client.get_latest_stats(
            resource_id=args.get("resource_id", ""),
            stat_keys=args.get("stat_keys"),
        )
        if not stats:
            return ActionResult(success=True, summary="No current metric values returned", raw={})
        return ActionResult(
            success=True,
            summary=f"Latest values for {len(stats)} metric(s)",
            raw=stats,
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


async def _vrops_get_stats(args: dict) -> ActionResult:
    try:
        client = _build_client(args)
        stat_keys = args.get("stat_keys") or []
        if not stat_keys:
            return ActionResult(success=False, summary="stat_keys is required (use vrops_get_stat_keys to discover them)")
        summary = client.get_stats(
            resource_id=args.get("resource_id", ""),
            stat_keys=stat_keys,
            hours_back=args.get("hours_back", 6),
            rollup=args.get("rollup", "AVG"),
        )
        if not summary:
            return ActionResult(success=True, summary="No metric data in the requested window", raw={})
        return ActionResult(
            success=True,
            summary=f"Time-series summary for {len(summary)} metric(s) over {args.get('hours_back', 6)}h",
            raw=summary,
        )
    except Exception as e:
        return ActionResult(success=False, summary=str(e))


# ---------------------------------------------------------------------------
# Public registry of all vROps actions
# ---------------------------------------------------------------------------

vrops_actions: list[ActionDefinition] = [
    ActionDefinition(
        name="vrops_get_version",
        description="Get the vRealize Operations Manager version.",
        input_schema={"type": "object", "properties": {}},
        handler=_vrops_get_version,
    ),
    ActionDefinition(
        name="vrops_find_resource",
        description="Find a vROps resource by name and kind (e.g. HostSystem, VirtualMachine). returns the resource id to be use for fetch properties",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Resource name to search for"},
                "resource_kind": {
                    "type": "string",
                    "description": "Resource kind (e.g. HostSystem, VirtualMachine, VMwareAdapter Instance)",
                    "default": "VirtualMachine",
                },
                "adapter_kind": {
                    "type": "string",
                    "description": "Adapter kind (e.g. VMWARE, NSXTAdapter)",
                    "default": "VMWARE",
                },
            },
            "required": ["name"],
        },
        handler=_vrops_find_resource,
    ),
    ActionDefinition(
        name="vrops_get_resource_properties",
        description="Get properties for a specific vROps resource by its ID.",
        input_schema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "vROps resource identifier"},
            },
            "required": ["resource_id"],
        },
        handler=_vrops_get_resource_properties,
    ),
    ActionDefinition(
        name="vrops_create_resource",
        description="Create a new resource in vROps.",
        input_schema={
            "type": "object",
            "properties": {
                "adapter_kind": {"type": "string", "description": "Adapter kind key (e.g. ARCH_COMPLIANCE)"},
                "resource_kind": {"type": "string", "description": "Resource kind key (e.g. Certificate, License)"},
                "name": {"type": "string", "description": "Resource display name"},
                "identifiers": {
                    "type": "object",
                    "description": "Key-value pairs of resource identifiers",
                    "default": {},
                },
                "description": {"type": "string", "description": "Resource description", "default": ""},
            },
            "required": ["adapter_kind", "resource_kind", "name"],
        },
        handler=_vrops_create_resource,
    ),
    ActionDefinition(
        name="vrops_push_properties",
        description="Push properties/statistics to a vROps resource.",
        input_schema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "vROps resource identifier"},
                "properties": {
                    "type": "object",
                    "description": "Key-value properties to push (e.g. {\"status\": \"compliant\"})",
                },
            },
            "required": ["resource_id", "properties"],
        },
        handler=_vrops_push_properties,
    ),
    ActionDefinition(
        name="vrops_push_event",
        description="Push an event/alert to a vROps resource.",
        input_schema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "vROps resource identifier"},
                "event_type": {"type": "string", "description": "Event type (e.g. INFO, WARNING, ERROR)", "default": "INFO"},
                "message": {"type": "string", "description": "Event message"},
                "severity": {
                    "type": "string",
                    "description": "Event severity",
                    "enum": ["INFO", "WARNING", "ERROR", "CRITICAL"],
                    "default": "WARNING",
                },
            },
            "required": ["resource_id", "event_type", "message"],
        },
        handler=_vrops_push_event,
    ),
    ActionDefinition(
        name="vrops_get_monitored_vcenters",
        description="List vCenter Servers monitored by vROps.",
        input_schema={"type": "object", "properties": {}},
        handler=_vrops_get_monitored_vcenters,
    ),
    ActionDefinition(
        name="vrops_get_monitored_nsxt_managers",
        description="List NSX-T managers monitored by vROps.",
        input_schema={"type": "object", "properties": {}},
        handler=_vrops_get_monitored_nsxt,
    ),
    ActionDefinition(
        name="vrops_add_child_relationship",
        description="Add a child relationship between two vROps resources.",
        input_schema={
            "type": "object",
            "properties": {
                "parent_id": {"type": "string", "description": "Parent resource identifier"},
                "child_id": {"type": "string", "description": "Child resource identifier"},
            },
            "required": ["parent_id", "child_id"],
        },
        handler=_vrops_add_child_relationship,
    ),
    # --- Read: alerts / health / performance ---
    ActionDefinition(
        name="vrops_search_resources",
        description=(
            "Search vROps resources by (partial) name. Returns ALL matches with "
            "their resource IDs, kind, and current health. Use this first to get "
            "the resource_id needed by the health/stats/alerts tools."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Resource name or substring to search for"},
                "resource_kind": {"type": "string", "description": "Optional filter, e.g. HostSystem, VirtualMachine, Datastore, ClusterComputeResource"},
                "adapter_kind": {"type": "string", "description": "Optional adapter filter, e.g. VMWARE"},
            },
            "required": ["name"],
        },
        handler=_vrops_search_resources,
    ),
    ActionDefinition(
        name="vrops_get_resource_health",
        description="Get the current health (GREEN/YELLOW/ORANGE/RED), health value (0-100), and status states for a resource by ID.",
        input_schema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "vROps resource identifier"},
            },
            "required": ["resource_id"],
        },
        handler=_vrops_get_resource_health,
    ),
    ActionDefinition(
        name="vrops_get_alerts",
        description=(
            "Summarize active alerts. Optionally filter by resource_id and/or "
            "criticality. Returns a COMPLETE compact summary: accurate total, "
            "breakdown by criticality and status, the most common alert names, "
            "and the most-severe alerts in detail (the 'top' list, each with the "
            "affected object's resourceName and resourceKind). Report the total "
            "and breakdown, and refer to objects by resourceName (not resourceId); "
            "do not claim the 'top' list is the full set. "
            "Use vrops_get_alert with an alertId for full detail on one alert."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "Optional: only alerts for this resource"},
                "criticality": {
                    "type": "string",
                    "description": "Optional severity filter",
                    "enum": ["INFORMATION", "WARNING", "IMMEDIATE", "CRITICAL"],
                },
                "active_only": {"type": "boolean", "description": "Only active (uncancelled) alerts", "default": True},
            },
        },
        handler=_vrops_get_alerts,
    ),
    ActionDefinition(
        name="vrops_get_alert",
        description="Get full detail for a single alert by its alert ID.",
        input_schema={
            "type": "object",
            "properties": {
                "alert_id": {"type": "string", "description": "Alert identifier"},
            },
            "required": ["alert_id"],
        },
        handler=_vrops_get_alert,
    ),
    ActionDefinition(
        name="vrops_get_stat_keys",
        description=(
            "Discover which performance metric (stat) keys are available for a "
            "resource, e.g. 'cpu|usage_average', 'mem|usage_average'. Use this "
            "when you don't know the exact key needed for vrops_get_stats."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "vROps resource identifier"},
            },
            "required": ["resource_id"],
        },
        handler=_vrops_get_stat_keys,
    ),
    ActionDefinition(
        name="vrops_get_latest_stats",
        description=(
            "Get the most recent value of performance metrics for a resource. Use "
            "this for any 'resource consumption / utilization / current usage' "
            "question. Common keys: cpu|usage_average (CPU %), mem|usage_average "
            "(memory %), mem|consumed_average (KB), disk|usage_average (KBps), "
            "net|usage_average (KBps)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "vROps resource identifier"},
                "stat_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Metric keys to fetch, e.g. ['cpu|usage_average','mem|usage_average','disk|usage_average']. Omit to get all available metrics.",
                },
            },
            "required": ["resource_id"],
        },
        handler=_vrops_get_latest_stats,
    ),
    ActionDefinition(
        name="vrops_get_stats",
        description=(
            "Get a time-series summary (count/latest/min/max/avg) for performance "
            "metrics over a recent window. Best for trend / 'over the last N hours' "
            "questions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "resource_id": {"type": "string", "description": "vROps resource identifier"},
                "stat_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Metric keys, e.g. ['cpu|usage_average']",
                },
                "hours_back": {"type": "number", "description": "How many hours back to look", "default": 6},
                "rollup": {
                    "type": "string",
                    "description": "Roll-up function",
                    "enum": ["AVG", "MAX", "MIN", "SUM", "LATEST"],
                    "default": "AVG",
                },
            },
            "required": ["resource_id", "stat_keys"],
        },
        handler=_vrops_get_stats,
    ),
]