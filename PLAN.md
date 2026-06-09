# Implementation Plan: Robust dual-provider vROps harness

Goal: make the Slack → LLM → vROps harness stop hallucinating and stop returning
"No response.", add real alert/health/performance read capability, and add token
+ guardrail controls. Must keep supporting **both** OpenAI and local `qwen3:4b`
(provider chosen at runtime via `LLM_PROVIDER`).

## Design principle

One code path, provider-aware behaviour via a small capability flag on `LlmConfig`:

```
LlmConfig(
  provider: "openai" | "ollama",
  model, api_key, base_url, system_prompt,
  is_thinking_model: bool,      # qwen3 -> True: strip <think>, expect empty content
  max_output_tokens: int,       # both providers
  request_timeout_s: float,     # both providers
  max_tool_iterations: int = 5,
)
```

Everything below works for both providers; `is_thinking_model` / `provider`
gate only the qwen3-specific quirks.

---

## Phase 0 - Secrets (do first)

- **Manual (you):** rotate the Slack bot token, signing secret, and app token
  exposed in `.env.example:2-21` (regenerate in api.slack.com -> the app).
- **Code:** replace those values in `.env.example` with placeholders
  (`xoxb-REPLACE-ME`, etc.). Verify `.gitignore` lists `.env`.
- **Acceptance:** no real credential strings anywhere in the repo.

---

## Phase 1 - Fix the response loop (`src/ai/llm.py`, `src/config/types.py`)

Root cause of both "No response." and most hallucination. Rewrite
`process_with_llm` as a bounded agentic loop.

1. **Loop, don't one-shot.** Up to `max_tool_iterations` rounds; pass `tools=`
   on EVERY call so `find_resource -> get_resource_properties` chains work.
   Today `llm.py:140` drops `tools` on the 2nd call - remove that limitation.
2. **Correct OpenAI message shape.** Emit a single assistant message carrying
   the full `tool_calls` array, then one `tool` message per call - instead of
   the current N-separate-assistant-messages pattern (`llm.py:109-134`) that
   400s on parallel calls.
3. **Add `max_tokens` + `timeout`** to both completion calls.
4. **Empty-content handling (qwen3):** strip `<think>...</think>` before using
   content. If still empty AND no tool calls, do ONE corrective retry
   ("Summarize the tool results for the user in plain text"); only then fall
   back to a friendly message - never the bare "No response.".
5. **Loop exit:** stop when the model returns content with no tool calls, or
   iterations exhausted (then force a final text-only summarization call).

**Type change (`src/config/types.py`):** add a `ToolCall` dataclass and let
`Message` carry `tool_calls: list[ToolCall] | None` so one assistant message can
hold several calls. Update `_to_openai_messages` accordingly.

**Acceptance:** "what's the health of host X" triggers `find_resource` then a
stats/health call in one turn and returns grounded text; parallel tool calls no
longer error; qwen3 no longer returns "No response.".

---

## Phase 2 - Real read capability for alerts/health/performance

The client (`vrops_client.py`) is compliance-/push-oriented; it has no read path
for what users ask. Add client methods + actions:

| New client method | vROps endpoint | New action |
|---|---|---|
| `get_alerts(severity?, status?, resource_id?, page_size)` | `GET /api/alerts` | `vrops_get_alerts` |
| `get_resource_stats(resource_id, stat_keys[], since?)` | `POST /api/resources/stats/query` or `GET /api/resources/{id}/stats` | `vrops_get_resource_stats` |
| `get_latest_stats(resource_id, stat_keys[])` | `GET /api/resources/{id}/stats/latest` | `vrops_get_latest_stats` |
| `get_resource_health(resource_id)` | `GET /api/resources/{id}` (health/state) | `vrops_get_resource_health` |
| (optional) `search_resources(name, kind?)` | `GET /api/resources` | `vrops_search_resources` (multi-match; `find_resource` only returns `[0]`) |

Each action returns `ActionResult` with a compact `summary` and a bounded `raw`
(see Phase 3 truncation). Tool descriptions must be explicit (e.g. "Returns
active alerts with severity, status, and triggering resource") so the model
selects the right one instead of guessing.

**Acceptance:** "show me critical alerts" and "CPU usage of VM web-01" each
resolve to a real API call with real data.

---

## Phase 3 - Guardrails & token control

**Token / cost (both providers):**
- `max_output_tokens` (config, default ~800) on every completion.
- Truncate tool output before it re-enters context - replaces the raw
  `json.dumps(result.__dict__)` at `llm.py:131`. Cap each tool result
  (e.g. 4 KB / N list items); when truncating a list, keep top N and append
  "...(+M more, ask to narrow)". Biggest token lever for vROps payloads.
- Per-thread token budget logged each turn (use API `usage` field).

**Grounding (anti-hallucination):** rewrite system prompt (config + `.env.example`)
to: answer only from tool results; never invent metric values, alert text, or
resource names; if a tool returns nothing or errors, say so plainly; ask to
disambiguate when a name matches multiple resources.

**vROps auth resilience (`actions.py` / `vrops_client.py`):** on 401, drop the
cached client (`actions.py:20-28`), re-authenticate once, retry. Token currently
never refreshes -> guaranteed failures on a long-running bot.

**Retry hygiene (`src/utils/retry.py`):** don't retry non-retryable errors -
skip HTTP 4xx (esp. 400/401/403) and json decode errors; keep retrying
429/5xx/timeouts. `retry.py:31` currently retries everything.

**Token-safe memory pruning (`src/memory/memory.py`):** the count-based trim at
`memory.py:39-42` can orphan a `tool` message from its assistant `tool_calls`
parent -> API 400. Prune in complete turns (never split an assistant-tool-call
group from its tool replies).

**Acceptance:** large dumps don't blow the context; no 400s from pruning;
expired vROps tokens self-heal; bot says "no alerts found" instead of inventing.

---

## Phase 4 - Polish & verification

- **Slack UX:** post a quick "working on it..." ack in `_process_text`
  (`bot.py:51`) before the multi-step loop. Consider a shared event loop instead
  of `new_event_loop()` per message (`bot.py:69`).
- **Tests (`tests/`):** unit tests for (a) loop driving 2 sequential calls,
  (b) correct multi-tool message shaping, (c) `<think>` stripping, (d) tool-output
  truncation, (e) memory pruning keeping tool pairs intact. Mock the OpenAI client
  so tests need no network.
- **README/.env:** document new actions, `MAX_OUTPUT_TOKENS`, `REQUEST_TIMEOUT_S`,
  `MAX_TOOL_ITERATIONS`, and the thinking-model flag.

---

## Risk notes for the dual-provider requirement

- qwen3:4b remains the weakest link even after this - small models are
  unreliable at multi-step tool selection. The plan maximizes its odds
  (think-stripping, sequential loop, tight tool descriptions, small payloads,
  grounding prompt), but gpt-4o (or a larger local model) will be materially more
  accurate for real alert/health answers. Keep qwen3 for dev/offline, gpt-4o for
  real use.
- Ollama's OpenAI-compatible tool-calling is less consistent than OpenAI's; the
  bounded loop + forced final summarization call is the safety net for malformed/
  empty tool calls.

---

## Recommended execution order

1. Phase 0 + Phase 1 (stop the bleeding: secrets + response loop).
2. Phase 2 (add alerts/health/perf read actions).
3. Phase 3 (guardrails + token control).
4. Phase 4 (polish + tests).
