"""Unit tests for the robustness/guardrail changes (no network required)."""

from __future__ import annotations

from src.ai.llm import (
    _strip_think,
    _bound_raw,
    _format_tool_result,
    _to_openai_messages,
    _effective_system_prompt,
    LlmConfig,
    MAX_TOOL_LIST_ITEMS,
)
from src.ai.anthropic_llm import _to_anthropic_messages
from src.config.types import Message, ToolCall, ActionResult
from src.memory.memory import ConversationMemory
from src.actions.registry import ActionRegistry
from src.actions.builtin.vrops.actions import vrops_actions


# --- think-tag stripping (qwen3) ---------------------------------------------
def test_strip_think_removes_block():
    assert _strip_think("<think>reasoning here</think>Hello") == "Hello"


def test_strip_think_multiline_and_case():
    txt = "<THINK>\nstep 1\nstep 2\n</THINK>  Answer: 42"
    assert _strip_think(txt) == "Answer: 42"


def test_strip_think_handles_none_and_empty():
    assert _strip_think(None) == ""
    assert _strip_think("") == ""


def test_strip_think_drops_truncated_unclosed_block():
    # Model hit the token cap mid-thought: opening tag, no close, no answer.
    assert _strip_think("Answer first.<think>reasoning cut off") == "Answer first."
    assert _strip_think("<think>only reasoning, truncated") == ""


def test_effective_system_prompt_disables_thinking_for_thinking_models():
    cfg = LlmConfig(api_key="x", model="qwen3:4b", system_prompt="base", is_thinking_model=True)
    assert "/no_think" in _effective_system_prompt(cfg)

    cfg2 = LlmConfig(api_key="x", model="gpt-4o", system_prompt="base", is_thinking_model=False)
    assert _effective_system_prompt(cfg2) == "base"


# --- tool-output bounding / truncation ---------------------------------------
def test_bound_raw_caps_long_lists():
    raw = list(range(100))
    bounded = _bound_raw(raw)
    assert len(bounded) == MAX_TOOL_LIST_ITEMS + 1
    assert "more" in str(bounded[-1])


def test_bound_raw_passes_small_payloads():
    assert _bound_raw({"a": 1}) == {"a": 1}
    assert _bound_raw([1, 2, 3]) == [1, 2, 3]


def test_bound_raw_caps_large_dicts():
    raw = {f"k{i}": i for i in range(100)}
    bounded = _bound_raw(raw)
    assert len(bounded) == MAX_TOOL_LIST_ITEMS + 1  # +1 for the __truncated__ marker
    assert "__truncated__" in bounded


def test_format_tool_result_truncates_huge_payload():
    big = ActionResult(success=True, summary="ok", raw={"blob": "x" * 10_000})
    out = _format_tool_result(big)
    assert "truncated" in out
    assert len(out) < 5_000


# --- OpenAI message shaping (parallel tool calls in ONE assistant msg) --------
def test_to_openai_messages_groups_parallel_tool_calls():
    history = [
        Message(role="user", content="check both"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="a", name="vrops_get_alerts", arguments="{}"),
                ToolCall(id="b", name="vrops_get_resource_health", arguments="{}"),
            ],
        ),
        Message(role="tool", content='{"ok":1}', tool_call_id="a"),
        Message(role="tool", content='{"ok":2}', tool_call_id="b"),
    ]
    msgs = _to_openai_messages(history, "sys")

    assistant = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant) == 1  # both calls in a single assistant message
    assert len(assistant[0]["tool_calls"]) == 2
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"a", "b"}


# --- memory pruning never orphans a tool message -----------------------------
def test_pruning_keeps_user_turn_boundary():
    mem = ConversationMemory(max_turns=4)
    ch, ts = "C1", None

    # Two full turns; the first should be pruned out as a whole.
    mem.append(ch, ts, Message(role="user", content="turn1"))
    mem.append(ch, ts, Message(role="assistant", content="", tool_calls=[ToolCall("x", "t", "{}")]))
    mem.append(ch, ts, Message(role="tool", content="r", tool_call_id="x"))
    mem.append(ch, ts, Message(role="assistant", content="done1"))
    mem.append(ch, ts, Message(role="user", content="turn2"))
    mem.append(ch, ts, Message(role="assistant", content="done2"))

    hist = mem.get_history(ch, ts)
    # First retained message must be a clean user boundary, never a dangling tool.
    assert hist[0].role == "user"
    assert all(
        not (m.role == "tool") or any(
            h.role == "assistant" and h.tool_calls for h in hist[:i]
        )
        for i, m in enumerate(hist)
    )


# --- Anthropic (Claude) provider conversion -----------------------------------
def test_anthropic_tools_use_input_schema():
    reg = ActionRegistry()
    for a in vrops_actions:
        reg.register(a)
    tools = reg.to_anthropic_tools()
    assert tools and all({"name", "description", "input_schema"} <= set(t) for t in tools)
    # Anthropic must NOT have the OpenAI "function" wrapper.
    assert all("function" not in t and "parameters" not in t for t in tools)


def test_anthropic_messages_group_tool_use_and_coalesce_results():
    history = [
        Message(role="user", content="check both"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="a", name="vrops_get_alerts", arguments="{}"),
                ToolCall(id="b", name="vrops_get_resource_health", arguments='{"resource_id":"x"}'),
            ],
        ),
        Message(role="tool", content='{"ok":1}', tool_call_id="a"),
        Message(role="tool", content='{"ok":2}', tool_call_id="b"),
    ]
    msgs = _to_anthropic_messages(history)

    # assistant turn carries both tool_use blocks (no text block, content was empty)
    assistant = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant) == 1
    tool_use_blocks = [b for b in assistant[0]["content"] if b["type"] == "tool_use"]
    assert {b["id"] for b in tool_use_blocks} == {"a", "b"}
    assert tool_use_blocks[1]["input"] == {"resource_id": "x"}  # arguments parsed to dict

    # both tool results coalesced into ONE following user turn
    result_turns = [
        m for m in msgs
        if m["role"] == "user" and isinstance(m["content"], list)
        and m["content"][0].get("type") == "tool_result"
    ]
    assert len(result_turns) == 1
    assert {b["tool_use_id"] for b in result_turns[0]["content"]} == {"a", "b"}
