"""vROps action definitions — expose VropsClient methods as LLM-callable tools."""

from __future__ import annotations

from ....config.types import ActionDefinition, ActionResult
from .vrops_client import VropsClient
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
]