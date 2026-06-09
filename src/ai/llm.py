"""AI / LLM integration layer.

Currently supports OpenAI-compatible APIs. Designed so the provider can be
swapped (e.g. Anthropic, local Ollama) behind the same interface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from openai import AsyncOpenAI

from ..config.types import Message
from ..actions.registry import ActionRegistry
from ..memory.memory import ConversationMemory
from ..utils.logger import info, debug
from ..utils.retry import with_retry, RetryOptions


@dataclass
class LlmConfig:
    api_key: str
    model: str
    system_prompt: str
    base_url: str | None = None


# ---------------------------------------------------------------------------
# Helpers to convert our Message structs to OpenAI API dicts
# ---------------------------------------------------------------------------
def _to_openai_messages(history: list[Message], system_prompt: str) -> list[dict]:
    msgs: list[dict] = []

    # System prompt first
    msgs.append({"role": "system", "content": system_prompt})

    for m in history:
        if m.role == "system":
            continue  # already injected above
        elif m.role == "user":
            msgs.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            entry: dict = {"role": "assistant", "content": m.content}
            if m.tool_call_id:
                entry["tool_calls"] = [
                    {
                        "id": m.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": m.tool_name or "",
                            "arguments": m.content,
                        },
                    }
                ]
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


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------
async def process_with_llm(
    user_message: str,
    channel: str,
    thread_ts: str | None,
    memory: ConversationMemory,
    registry: ActionRegistry,
    config: LlmConfig,
) -> str:
    client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)

    # 1. Append user message
    memory.append(channel, thread_ts, Message(role="user", content=user_message))

    # 2. Build message list
    history = memory.get_history(channel, thread_ts)
    messages = _to_openai_messages(history, config.system_prompt)

    tools = registry.to_openai_tools()

    info("Calling LLM", model=config.model, tool_count=len(tools))

    # 3. First LLM call
    response = await with_retry(
        lambda: client.chat.completions.create(
            model=config.model,
            messages=messages,  # type: ignore[arg-type]
            tools=tools if tools else None,
            tool_choice="auto" if tools else None,
        ),
        RetryOptions(),
    )
    choice = response.choices[0]
    if not choice:
        raise RuntimeError("LLM returned no choices")

    assistant_msg = choice.message

    # 4. Handle tool calls
    if assistant_msg.tool_calls:
        for tc in assistant_msg.tool_calls:
            memory.append(
                channel,
                thread_ts,
                Message(
                    role="assistant",
                    content=tc.function.arguments,
                    tool_call_id=tc.id,
                    tool_name=tc.function.name,
                ),
            )

        # Execute each tool call
        for tc in assistant_msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            result = await registry.execute(tc.function.name, args)
            memory.append(
                channel,
                thread_ts,
                Message(
                    role="tool",
                    content=json.dumps(result.__dict__),
                    tool_call_id=tc.id,
                ),
            )

        # 5. Second LLM call with tool results
        updated_history = memory.get_history(channel, thread_ts)
        second_msgs = _to_openai_messages(updated_history, config.system_prompt)

        final_response = await with_retry(
            lambda: client.chat.completions.create(
                model=config.model,
                messages=second_msgs,  # type: ignore[arg-type]
            ),
            RetryOptions(),
        )

        final_text = final_response.choices[0].message.content or "No response."
        memory.append(
            channel, thread_ts, Message(role="assistant", content=final_text)
        )
        return final_text

    # 6. Plain text response
    text = assistant_msg.content or "No response."
    memory.append(channel, thread_ts, Message(role="assistant", content=text))
    return text