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
    provider: str = "openai"
    base_url: str | None = None
    is_thinking_model: bool = False
    max_output_tokens: int = 800
    request_timeout_s: float = 60.0
    max_tool_iterations: int = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _strip_think(text: str | None) -> str:
    """Remove <think>...</think> reasoning emitted by qwen3-style models.

    Handles both closed blocks and an *unterminated* opening tag (which happens
    when the model is truncated by the token cap mid-thought) — in that case
    everything from ``<think>`` onward is reasoning, not an answer.
    """
    if not text:
        return ""
    cleaned = _THINK_RE.sub("", text)
    open_idx = cleaned.lower().find("<think>")
    if open_idx != -1:
        cleaned = cleaned[:open_idx]
    return cleaned.strip()


def _effective_system_prompt(config: LlmConfig) -> str:
    """For thinking models, disable chain-of-thought so the token budget is
    spent on the answer, not on reasoning that gets truncated. qwen3 honours the
    ``/no_think`` soft switch placed in the system prompt."""
    if config.is_thinking_model:
        return f"{config.system_prompt}\n\n/no_think"
    return config.system_prompt


def _bound_raw(raw):
    """Cap large list/dict payloads before they re-enter the context window."""
    if isinstance(raw, list) and len(raw) > MAX_TOOL_LIST_ITEMS:
        kept = raw[:MAX_TOOL_LIST_ITEMS]
        return kept + [f"...(+{len(raw) - MAX_TOOL_LIST_ITEMS} more, ask to narrow the query)"]
    if isinstance(raw, dict) and len(raw) > MAX_TOOL_LIST_ITEMS:
        items = list(raw.items())[:MAX_TOOL_LIST_ITEMS]
        bounded = dict(items)
        bounded["__truncated__"] = (
            f"+{len(raw) - MAX_TOOL_LIST_ITEMS} more keys; request specific "
            "stat_keys to narrow"
        )
        return bounded
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
            finish_reason=_finish_reason(response),
        )


def _finish_reason(response) -> str | None:
    try:
        return response.choices[0].finish_reason
    except (AttributeError, IndexError):
        return None


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


def _effective_max_tokens(config: LlmConfig) -> int:
    """Thinking models need headroom for reasoning *and* the answer; if the build
    ignores /no_think they otherwise get truncated mid-thought. Give them a floor."""
    if config.is_thinking_model:
        return max(config.max_output_tokens, 2_048)
    return config.max_output_tokens


def _create(client: AsyncOpenAI, config: LlmConfig, messages: list[dict], tools):
    """Build the completion coroutine factory (re-invoked by with_retry)."""
    kwargs: dict = {
        "model": config.model,
        "messages": messages,
        "max_tokens": _effective_max_tokens(config),
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    # Belt-and-suspenders for Ollama thinking models: also disable thinking via
    # the native API flag (the /no_think prompt switch is the primary mechanism).
    if config.provider == "ollama" and config.is_thinking_model:
        kwargs["extra_body"] = {"think": False}
    return lambda: client.chat.completions.create(**kwargs)  # type: ignore[arg-type]


def _inject_no_think(messages: list[dict]) -> None:
    """Append the qwen3 /no_think switch to the latest user turn (more reliably
    honoured by some Ollama templates than the system prompt alone)."""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content") or ""
            if "/no_think" not in content:
                m["content"] = f"{content} /no_think".strip()
            break


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
    sys_prompt = _effective_system_prompt(config)

    truncated = False
    for iteration in range(config.max_tool_iterations):
        messages = _to_openai_messages(
            memory.get_history(channel, thread_ts), sys_prompt
        )
        if config.is_thinking_model:
            _inject_no_think(messages)
        info("Calling LLM", model=config.model, iteration=iteration, tool_count=len(tools))

        response = await with_retry(
            _create(client, config, messages, tools), RetryOptions()
        )
        _log_usage(response)

        if not response.choices:
            raise RuntimeError("LLM returned no choices")
        msg = response.choices[0].message
        content = _strip_think(msg.content)
        if _finish_reason(response) == "length":
            truncated = True
            warn(
                "LLM output truncated by token limit before finishing",
                iteration=iteration,
                model=config.model,
            )

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
        memory.get_history(channel, thread_ts), sys_prompt
    )
    nudge = (
        "Summarize the results so far for the user in plain, concise text. "
        "Use only information from the tool results above; do not call any "
        "tools and do not invent data."
    )
    if config.is_thinking_model:
        nudge += " /no_think"
    messages.append({"role": "user", "content": nudge})

    final = await with_retry(_create(client, config, messages, None), RetryOptions())
    _log_usage(final)
    if _finish_reason(final) == "length":
        truncated = True
    text = _strip_think(final.choices[0].message.content)
    if not text:
        if truncated:
            text = (
                "⚠️ The model ran out of its output budget before finishing — it "
                "spent the tokens reasoning. Increase MAX_OUTPUT_TOKENS, or switch "
                "to a more capable model (e.g. OPENAI_MODEL=gpt-4o)."
            )
        else:
            text = "I wasn't able to produce a response for that. Please try rephrasing your request."
    memory.append(channel, thread_ts, Message(role="assistant", content=text))
    return text
