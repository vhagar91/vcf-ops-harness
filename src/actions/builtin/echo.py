"""Built-in "echo" action for testing."""

from __future__ import annotations

from ...config.types import ActionDefinition, ActionResult


async def _echo_handler(args: dict) -> ActionResult:
    msg = str(args.get("message", ""))
    return ActionResult(success=True, summary=f"Echo: {msg}", raw={"echoed": msg})


echo_action = ActionDefinition(
    name="echo",
    description="Echoes back the input. Useful for testing connectivity.",
    input_schema={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message to echo back",
            },
        },
        "required": ["message"],
    },
    handler=_echo_handler,
)