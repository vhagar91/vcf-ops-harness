"""Pluggable action registry.

Actions are registered by name and can be invoked by the LLM via tool calls.
"""

from __future__ import annotations

from ..config.types import ActionDefinition, ActionResult
from ..utils import logger


class ActionRegistry:
    """Registry of named action plugins."""

    def __init__(self) -> None:
        self._actions: dict[str, ActionDefinition] = {}

    def register(self, action: ActionDefinition) -> None:
        if action.name in self._actions:
            logger.warn(f"Overwriting existing action: {action.name}")
        self._actions[action.name] = action
        logger.info(f"Registered action: {action.name} — {action.description}")

    def get(self, name: str) -> ActionDefinition | None:
        return self._actions.get(name)

    def list(self) -> list[ActionDefinition]:
        return list(self._actions.values())

    def to_openai_tools(self) -> list[dict]:
        """Return registered actions as OpenAI-compatible tool definitions."""
        tools: list[dict] = []
        for a in self._actions.values():
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": a.name,
                        "description": a.description,
                        "parameters": a.input_schema,
                    },
                }
            )
        return tools

    async def execute(self, name: str, args: dict) -> ActionResult:
        action = self._actions.get(name)
        if not action:
            return ActionResult(success=False, summary=f"Unknown action: {name}")
        try:
            logger.info(f"Executing action: {name}", args=args)
            return await action.handler(args)
        except Exception as exc:
            msg = str(exc)
            logger.error(f"Action {name} failed", error=msg)
            return ActionResult(
                success=False, summary=f'Action "{name}" failed: {msg}'
            )