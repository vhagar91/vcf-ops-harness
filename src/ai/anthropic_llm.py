"""Anthropic (Claude) provider — native SDK integration.

Claude's Messages API differs from the OpenAI shape: the system prompt is a
top-level parameter, tools use ``input_schema`` directly, and responses are
lists of content blocks (text / tool_use). This module mirrors the bounded
agentic loop in :mod:`src.ai.llm` but speaks the native Anthropic format.

The ``anthropic`` package is imported lazily inside :func:`process_with_anthropic`
so the pure conversion helpers (and the rest of the harness) import without the
dependency installed.
"""

from __future__ import annotations

import json

from ..config.types import Message, ToolCall
from ..actions.registry import ActionRegistry
from ..memory.memory import ConversationMemory
from ..utils.logger import info, warn, debug
from ..utils.retry import with_retry, RetryOptions
from .llm import LlmConfig, _format_tool_result  # reuse guardrails


def _to_anthropic_messages(history: list[Message]) -> list[dict]:
    """Convert neutral history into Anthropic ``messages`` (content blocks).

    - assistant tool calls → an assistant turn with ``tool_use`` blocks
    - tool results → coalesced into a single following ``user`` turn of
      ``tool_result`` blocks (Anthropic groups parallel results together)
    """
    msgs: list[dict] = []
    for m in history:
        if m.role == "system":
            continue
        if m.role == "user":
            msgs.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            blocks: list[dict] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls or []:
                try:
                    tool_input = json.loads(tc.arguments or "{}")
                except json.JSONDecodeError:
                    tool_input = {}
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tool_input}
                )
            if not blocks:
                blocks = [{"type": "text", "text": "(no content)"}]
            msgs.append({"role": "assistant", "content": blocks})
        elif m.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id or "",
                "content": m.content,
            }
            # Coalesce consecutive tool results into one user turn.
            prev = msgs[-1] if msgs else None
            if (
                prev
                and prev["role"] == "user"
                and isinstance(prev["content"], list)
                and prev["content"]
                and isinstance(prev["content"][0], dict)
                and prev["content"][0].get("type") == "tool_result"
            ):
                prev["content"].append(block)
            else:
                msgs.append({"role": "user", "content": [block]})
    return msgs


def _log_usage(response) -> None:
    usage = getattr(response, "usage", None)
    if usage:
        info(
            "Claude usage",
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_read=getattr(usage, "cache_read_input_tokens", None),
            stop_reason=getattr(response, "stop_reason", None),
        )


def _text_of(response) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


async def process_with_anthropic(
    user_message: str,
    channel: str,
    thread_ts: str | None,
    memory: ConversationMemory,
    registry: ActionRegistry,
    config: LlmConfig,
) -> str:
    # Lazy import so the module loads without the dependency present.
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=config.api_key, timeout=config.request_timeout_s)

    memory.append(channel, thread_ts, Message(role="user", content=user_message))
    tools = registry.to_anthropic_tools()

    def _create(extra_messages: list[dict] | None = None, with_tools: bool = True):
        msgs = _to_anthropic_messages(memory.get_history(channel, thread_ts))
        if extra_messages:
            msgs = msgs + extra_messages
        kwargs: dict = {
            "model": config.model,
            "max_tokens": config.max_output_tokens,
            "system": config.system_prompt,
            "messages": msgs,
        }
        # Note: extended thinking is intentionally omitted. Preserving Claude's
        # thinking blocks across tool-use round-trips would require provider-
        # native memory; the neutral history here can't round-trip them. Omitting
        # `thinking` runs Opus without it, which is correct for this loop.
        if with_tools and tools:
            kwargs["tools"] = tools
        return lambda: client.messages.create(**kwargs)

    truncated = False
    for iteration in range(config.max_tool_iterations):
        info("Calling Claude", model=config.model, iteration=iteration, tool_count=len(tools))
        response = await with_retry(_create(), RetryOptions())
        _log_usage(response)

        if response.stop_reason == "max_tokens":
            truncated = True
            warn("Claude output truncated by max_tokens", iteration=iteration, model=config.model)

        text = _text_of(response)
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if response.stop_reason == "tool_use" and tool_uses:
            tcs = [
                ToolCall(id=b.id, name=b.name, arguments=json.dumps(b.input or {}))
                for b in tool_uses
            ]
            memory.append(
                channel, thread_ts, Message(role="assistant", content=text, tool_calls=tcs)
            )
            for b in tool_uses:
                args = b.input if isinstance(b.input, dict) else {}
                result = await registry.execute(b.name, args)
                memory.append(
                    channel,
                    thread_ts,
                    Message(role="tool", content=_format_tool_result(result), tool_call_id=b.id),
                )
            continue  # loop with tool results, tools still available

        if text:
            memory.append(channel, thread_ts, Message(role="assistant", content=text))
            return text

        debug("Empty Claude content without tool use; forcing summarization")
        break

    # Forced final summarization (iterations exhausted or empty content), no tools.
    nudge = [
        {
            "role": "user",
            "content": (
                "Summarize the results so far for the user in plain, concise text. "
                "Use only information from the tool results above; do not call any "
                "tools and do not invent data."
            ),
        }
    ]
    final = await with_retry(_create(extra_messages=nudge, with_tools=False), RetryOptions())
    _log_usage(final)
    if getattr(final, "stop_reason", None) == "max_tokens":
        truncated = True
    text = _text_of(final)
    if not text:
        text = (
            "⚠️ The model ran out of its output budget before finishing. Increase "
            "MAX_OUTPUT_TOKENS."
            if truncated
            else "I wasn't able to produce a response for that. Please try rephrasing your request."
        )
    memory.append(channel, thread_ts, Message(role="assistant", content=text))
    return text
