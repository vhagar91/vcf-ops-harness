"""Built-in "get_time" action — returns the current server time."""

from __future__ import annotations

from datetime import datetime, timezone

from ...config.types import ActionDefinition, ActionResult


async def _time_handler(args: dict) -> ActionResult:
    fmt = str(args.get("format", "iso"))
    now = datetime.now(timezone.utc)

    if fmt == "unix":
        summary = str(int(now.timestamp()))
    elif fmt == "human":
        summary = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        summary = now.isoformat()

    return ActionResult(success=True, summary=summary, raw={"now": summary, "format": fmt})


get_time_action = ActionDefinition(
    name="get_time",
    description="Returns the current date and time on the server.",
    input_schema={
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "description": 'Optional: "iso" (default), "unix", or "human"',
                "enum": ["iso", "unix", "human"],
            },
        },
    },
    handler=_time_handler,
)