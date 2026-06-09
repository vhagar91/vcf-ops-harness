"""AI / LLM integration layer.

Supports OpenAI-compatible APIs (OpenAI and local Ollama). The same code path
serves both; provider-specific quirks (notably qwen3 "thinking" output) are
gated by flags on :class:`LlmConfig`.

The core entry point, :func:`process_with_llm`, runs a *bounded agentic loop*:
the model may call tools across several rounds (e.g. find a resource, then read
its stats) until it produces a final text answer or the iteration cap is hit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from openai import AsyncOpenAI

from ..config.types import Message, ToolCall, ActionResult
from ..actions.registry import ActionRegistry
from ..memory.memory import ConversationMemory
from ..utils.logger import info, debug, warn
from ..utils.retry import with_retry, RetryOptions


# Caps that protect the context window / token budget.
MAX_TOOL_RESULT_CHARS = 4_000
MAX_TOOL_LIST_ITEMS = 25

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


@dataclass
class LlmConfig:
    api_key: str
    model: str
    system_prompt: str
    base_url: str | None = None
    is_thinking_model: bool = False
    max_output_tokens: int = 800
    request_timeout_s: float = 60.0
    max_tool_iterations: int = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _strip_think(text: str | None) -> str:
    """Remove <think>...</think> reasoning blocks emitted by qwen3-style models."""
    if not text:
        return ""
    return _THINK_RE.sub("", text).strip()


def _bound_raw(raw):
    """Cap large list payloads before they re-enter the context window."""
    if isinstance(raw, list) and len(raw) > MAX_TOOL_LIST_ITEMS:
        kept = raw[:MAX_TOOL_LIST_ITEMS]
        return kept + [f"...(+{len(raw) - MAX_TOOL_LIST_ITEMS} more, ask to narrow the query)"]
    return raw


def _format_tool_result(result: ActionResult) -> str:
    """Serialize a tool result compactly, truncating oversized payloads."""
    payload: dict = {"success": result.success, "summary": result.summary}
    if result.raw is not None:
        payload["data"] = _bound_raw(result.raw)
    text = json.dumps(payload, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = (
            text[:MAX_TOOL_RESULT_CHARS]
            + f"... [truncated {len(text) - MAX_TOOL_RESULT_CHARS} chars; "
            "ask the user to narrow the query]"
        )
    return text


def _log_usage(response) -> None:
    usage = getattr(response, "usage", None)
    if usage:
        info(
            "LLM usage",
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
        )


def _to_openai_messages(history: list[Message], system_prompt: str) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": system_prompt}]

    for m in history:
        if m.role == "system":
            continue  # already injected above
        elif m.role == "user":
            msgs.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            entry: dict = {"role": "assistant"}
            if m.tool_calls:
                # Assistant requesting tools: content may be null; all calls go
                # in ONE message as a tool_calls array (OpenAI contract).
                entry["content"] = m.content or None
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in m.tool_calls
                ]
            else:
                entry["content"] = m.content
            msgs.append(entry)
        elif m.role == "tool":
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id or "",
                    "content": m.content,
                }
            )
    return msgs


def _create(client: AsyncOpenAI, config: LlmConfig, messages: list[dict], tools):
    """Build the completion coroutine factory (re-invoked by with_retry)."""
    kwargs: dict = {
        "model": config.model,
        "messages": messages,
        "max_tokens": config.max_output_tokens,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    return lambda: client.chat.completions.create(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Main orchestrator — bounded agentic loop
# ---------------------------------------------------------------------------
async def process_with_llm(
    user_message: str,
    channel: str,
    thread_ts: str | None,
    memory: ConversationMemory,
    registry: ActionRegistry,
    config: LlmConfig,
) -> str:
    client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.request_timeout_s,
    )

    memory.append(channel, thread_ts, Message(role="user", content=user_message))
    tools = registry.to_openai_tools()

    for iteration in range(config.max_tool_iterations):
        messages = _to_openai_messages(
            memory.get_history(channel, thread_ts), config.system_prompt
        )
        info("Calling LLM", model=config.model, iteration=iteration, tool_count=len(tools))

        response = await with_retry(
            _create(client, config, messages, tools), RetryOptions()
        )
        _log_usage(response)

        if not response.choices:
            raise RuntimeError("LLM returned no choices")
        msg = response.choices[0].message
        content = _strip_think(msg.content)

        # --- Tool calls requested: execute them, then loop with results ---
        if msg.tool_calls:
            tcs = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments or "{}",
                )
                for tc in msg.tool_calls
            ]
            memory.append(
                channel,
                thread_ts,
                Message(role="assistant", content=content or "", tool_calls=tcs),
            )

            for tc in tcs:
                try:
                    args = json.loads(tc.arguments or "{}")
                except json.JSONDecodeError:
                    warn("Tool arguments not valid JSON", tool=tc.name, raw=tc.arguments)
                    args = {}
                result = await registry.execute(tc.name, args)
                memory.append(
                    channel,
                    thread_ts,
                    Message(
                        role="tool",
                        content=_format_tool_result(result),
                        tool_call_id=tc.id,
                    ),
                )
            continue  # tools still available next round (sequential chains work)

        # --- No tool calls: a final text answer (if non-empty) ---
        if content:
            memory.append(channel, thread_ts, Message(role="assistant", content=content))
            return content

        # Empty content with no tool calls (common with thinking models):
        # fall through to a forced, tool-free summarization below.
        debug("Empty content without tool calls; forcing summarization")
        break

    # --- Forced final summarization (iterations exhausted or empty content) ---
    messages = _to_openai_messages(
        memory.get_history(channel, thread_ts), config.system_prompt
    )
    messages.append(
        {
            "role": "user",
            "content": (
                "Summarize the results so far for the user in plain, concise text. "
                "Use only information from the tool results above; do not call any "
                "tools and do not invent data."
            ),
        }
    )
    final = await with_retry(_create(client, config, messages, None), RetryOptions())
    _log_usage(final)
    text = _strip_think(final.choices[0].message.content) or (
        "I wasn't able to produce a response for that. Please try rephrasing your request."
    )
    memory.append(channel, thread_ts, Message(role="assistant", content=text))
    return text
