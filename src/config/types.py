"""Shared domain types used across the harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


@dataclass
class Message:
    """A single message in a conversation."""

    role: str  # 'system' | 'user' | 'assistant' | 'tool'
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass
class ActionResult:
    """Result returned by an action plugin."""

    success: bool
    summary: str
    raw: Any = None


@dataclass
class ActionDefinition:
    """Metadata about a registered action."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Coroutine[Any, Any, ActionResult]]


@dataclass
class PipelineEvent:
    """Envelope wrapping every event that flows through the pipeline."""

    channel: str
    user_id: str
    text: str
    thread_ts: str | None = None