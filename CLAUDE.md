# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the bot (needs a populated .env — see .env.example)
python3 -m src.main

# Tests
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -m pytest tests/test_robustness.py::test_strip_think_removes_block -v   # single test
```

There is no linter or formatter configured. Tests require no network (`test_robustness.py` exercises the pure conversion/guardrail helpers; `test_imports.py` is an import smoke test).

## Architecture

A Slack chat bot that routes natural-language messages through an LLM agentic loop, where the LLM can call **pluggable action tools** to read infrastructure data (primarily VMware vROps). Flow:

`Slack event` → `slack/bot.py` → `pipeline/orchestrator.py:run_pipeline` → provider runner → `actions/registry.py` → action handler → back to the LLM.

### The two-provider split is the central design fact

There are **two parallel LLM implementations** that both implement the *same bounded agentic loop* but speak different wire formats:

- `src/ai/llm.py` (`process_with_llm`) — OpenAI-compatible. Serves **both** `LLM_PROVIDER=openai` and `LLM_PROVIDER=ollama` (Ollama via the OpenAI-compat endpoint). Messages use the OpenAI shape (system message in the array, `tool_calls` array, `tool` role messages).
- `src/ai/anthropic_llm.py` (`process_with_anthropic`) — native Anthropic SDK. System prompt is a top-level param, tools use `input_schema`, responses are content blocks (`text`/`tool_use`), and tool results coalesce into a single following `user` turn.

`run_pipeline` picks the runner by `config.provider`. When changing loop behavior (iteration caps, summarization fallback, guardrails), **both files must be kept in sync** — they intentionally mirror each other. `anthropic_llm.py` reuses `_format_tool_result` and `LlmConfig` from `llm.py`.

### Neutral conversation format

`src/config/types.py` defines provider-agnostic `Message` and `ToolCall`. This is the canonical history stored in `ConversationMemory`. Each provider converts neutral history → its wire format on every call (`_to_openai_messages`, `_to_anthropic_messages`) — the conversation is never stored in a provider-specific shape. These converters are the most-tested code in the repo.

### Bounded agentic loop (shared pattern)

Each user message runs up to `MAX_TOOL_ITERATIONS` rounds. Per round the model may emit tool calls (chained sequentially across rounds, e.g. `vrops_search_resources` → get ID → `vrops_get_latest_stats`) or a final text answer. If iterations exhaust or the model returns empty content, a **forced summarization** call runs with tools disabled and a nudge to answer only from prior tool results. Guardrails applied throughout:
- Tool output is size-capped before re-entering context (`_bound_raw`, `_format_tool_result`, `MAX_TOOL_RESULT_CHARS`, `MAX_TOOL_LIST_ITEMS`).
- Replies capped at `MAX_OUTPUT_TOKENS`; truncation (`finish_reason`/`stop_reason`) is surfaced to the user instead of returning a silent empty reply.

### Thinking-model handling (qwen3 / Ollama)

`LLM_PROVIDER=ollama` with a model whose name matches `_THINKING_MODEL_HINTS` (qwen3, qwq, deepseek-r1, …) is auto-detected as a "thinking" model (override with `IS_THINKING_MODEL=true|false`). For these: `/no_think` is appended to the system prompt *and* the latest user turn, the native `think: False` flag is set, the token budget gets a floor (`_effective_max_tokens`), and `<think>…</think>` blocks (including unterminated ones from mid-thought truncation) are stripped via `_strip_think`. This handling lives only in `llm.py`; the Anthropic path deliberately omits extended thinking (see the comment in `_create`).

### Actions (tools)

An action is an `ActionDefinition` (name, description, JSON `input_schema`, async handler returning `ActionResult`). Register in `src/main.py` via `registry.register(...)`. The registry exposes them to both providers via `to_openai_tools()` / `to_anthropic_tools()`. Built-ins: `echo`, `get_time`, and the vROps tool suite in `src/actions/builtin/vrops/`. vROps actions lazily build+cache an authenticated `VropsClient` per server; absent `VROPS_*` env vars they return a credentials error (the bot still runs).
The `vrops_diagnose` action is a composite tool: it resolves a resource, then runs health + alerts + trend analysis + rule-based recommendations entirely in Python (`vrops/analysis.py` holds the pure logic) and returns one compact structured report, so weak models make a single tool call and only narrate the verdict.

### Memory pruning invariant

`ConversationMemory._prune` (`src/memory/memory.py`) trims to `max_turns` but the retained window **must start at a `user` message** — otherwise an assistant `tool_calls` message gets dropped while its `tool` replies are kept, which the OpenAI API rejects with a 400. Preserve this when touching pruning.

### Slack bot specifics

Bolt handlers are synchronous; each message spins up a fresh asyncio event loop to run the async pipeline. The bot posts a "🔎 Working on it…" ack first. Messages starting with `!`, bot messages, and `USLACKBOT` are ignored. Socket Mode is used when `SLACK_APP_TOKEN` is set, otherwise HTTP on `SLACK_PORT`. `/reset` clears memory for the channel/thread.

## Configuration

All config comes from environment variables loaded into `HarnessConfig` by `src/config/settings.py` (`load_config`). `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` are the only hard-required vars; provider keys are validated by the active provider path. The default `SYSTEM_PROMPT` (`DEFAULT_SYSTEM_PROMPT` in settings.py) is a vROps-grounding prompt that forbids inventing data and mandates fetching metrics via tools — edit it there, not inline. See `.env.example` and the README env table for the full list.
