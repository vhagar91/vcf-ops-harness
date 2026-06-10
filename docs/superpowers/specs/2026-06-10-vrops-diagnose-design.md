# Design: `vrops_diagnose` — reliable on-demand infra analysis

**Date:** 2026-06-10
**Status:** Approved (pending spec review)

## Problem

The harness is a Slack bot that answers infrastructure questions by routing
natural language through an LLM agentic loop that calls vROps tools. Two goals,
treated here as one tightly-coupled design:

1. **Capability** — assist in detecting infra issues via vROps, give
   recommendations, and analyze historical data.
2. **Reliability** — stop less-powerful models (qwen3 / Ollama) from
   hallucinating and producing truncated responses.

These interact: analysis tasks push more data through the model, which is
exactly when weak models break (mid-thought truncation, invented numbers,
derailed multi-step tool chaining).

## Guiding principle

**Push all analytical work into deterministic Python tools that return small,
pre-digested verdicts; the model only narrates.** A weak model cannot
hallucinate a trend it was handed as `"avg 82%, rising, breached 90% four
times"`, and there is little text to truncate. Reliability and capability
become the same lever.

## Scope

In scope (first effort):
- Health triage scan, historical trend analysis, rule-based recommendations —
  delivered as **one composite tool**.
- On-demand only (fits the existing message-driven bot; no scheduler).
- One reliability mechanism: **structured analysis output** + tight narration
  template.

Out of scope (future):
- Anomaly / baseline-deviation detection.
- Proactive scheduled scans.
- Exposing the three capabilities as individual sub-tools (approach C).
- Tool-call argument validation + retry.
- Env-configurable thresholds.

## Architecture: composite `vrops_diagnose`

One tool answers "how is resource X doing / any issues / what should I do?" It
performs triage → trend → recommendations entirely in Python and returns one
compact structured report. The model makes a **single** tool call and narrates
**one** result. No multi-step chaining for weak models to derail on.

The internals are factored as pure functions, so exposing the individual
capabilities later (approach C) is a cheap upgrade with no rework.

### Tool: `vrops_diagnose`

Input schema:

| Param | Required | Default | Notes |
|-------|----------|---------|-------|
| `name` | yes | — | Resource name to diagnose |
| `resource_kind` | no | `"VirtualMachine"` | |
| `adapter_kind` | no | — | |
| `hours_back` | no | `24` | Trend window |

Internal flow (deterministic):

1. **Resolve** via `vrops_search_resources`.
   - 0 matches → error result (`"not found"`).
   - >1 matches → return the match list so the model asks the user to
     disambiguate (never guess).
   - 1 match → proceed.
2. **Health** — current state (`GREEN|YELLOW|ORANGE|RED`) + value (0–100).
3. **Active alerts** — list with criticality, capped (e.g. top 10).
4. **Metrics** — a standard set fetched as a **raw time series** over
   `hours_back`:
   - `cpu|usage_average` (CPU %)
   - `mem|usage_average` (memory %)
   - `virtualDisk|totalLatency` (disk latency, ms)
   - `net|usage_average` (net, KBps)
   - `disk|usage_average` (disk throughput, KBps)

   For each metric, compute: `latest`, `min`, `max`, `avg`; **trend**
   (`rising|falling|stable` via least-squares slope, classified against a small
   fraction of the value range); **threshold breach** flag + breach count
   against configurable per-metric thresholds.
5. **Recommendations** — a rule engine maps detected conditions → ranked,
   canned remediation suggestions. Examples:
   - RED health or active critical alert → "investigate critical alert: …".
   - CPU avg > threshold and trend rising → "sustained high CPU, climbing;
     investigate runaway process or add vCPU".
   - Memory breach → memory-pressure advice (add RAM / check ballooning/leak).
   - Disk latency breach → "storage latency elevated; check datastore
     contention".
   - All clear → "healthy, no action needed".
6. **Verdict rollup** — overall `OK | WARNING | CRITICAL`.

> Note: the existing `vrops_get_stats` returns only a summary
> (`count/latest/min/max/avg`), which is insufficient for slope and breach
> count. `vrops_diagnose` needs the raw sample series — see "Client changes".

### Output (compact, bounded)

```json
{
  "resource": {"name": "web-01", "id": "...", "kind": "VirtualMachine"},
  "verdict": "WARNING",
  "health": {"state": "YELLOW", "value": 74},
  "active_alerts": [{"criticality": "WARNING", "message": "..."}],
  "metrics": [
    {"key": "cpu|usage_average", "label": "CPU %", "latest": 91.2,
     "avg": 82.0, "min": 60.0, "max": 95.0, "trend": "rising",
     "threshold": 90, "breached": true, "breach_count": 4}
  ],
  "recommendations": ["...", "..."],
  "window_hours": 24
}
```

All collections (alerts, metrics, recommendations) are capped, keeping the
payload well under `MAX_TOOL_RESULT_CHARS`. The `ActionResult.summary` carries a
one-line headline verdict.

## Module structure

- `src/actions/builtin/vrops/analysis.py` — **pure functions**, no network,
  heavily unit-tested:
  - `compute_trend(samples) -> "rising"|"falling"|"stable"`
  - `summarize_metric(key, samples, threshold) -> dict`
  - `evaluate_threshold(samples, threshold) -> (breached, breach_count)`
  - `build_recommendations(conditions) -> list[str]` (ranked)
  - `rollup_verdict(health, alerts, metrics) -> "OK"|"WARNING"|"CRITICAL"`
  - Default per-metric thresholds as documented constants.
- `src/actions/builtin/vrops/diagnose.py` — the `ActionDefinition` + async
  handler: orchestration and client I/O only; delegates all computation to
  `analysis.py`.
- Register the action in `src/main.py`.

### Client changes

`VropsClient` (or the diagnose handler via an existing lower-level call) must
fetch a **raw metric sample series** for the standard metric set over
`hours_back`. If the current client only exposes the summary-returning path, add
a method that returns the ordered samples per stat key.

## Reliability mechanism — structured analysis output

System-prompt additions:

1. **Routing** — direct "how is X / is X healthy / issues with X / recommend
   for X / analyze X" questions to `vrops_diagnose` as the primary path. The
   existing lower-level tools (`vrops_get_latest_stats`, `vrops_get_stats`,
   etc.) remain fully available for targeted metric queries — just not the
   primary path for diagnostic questions.
2. **Narration template** — a tight template the model fills: headline verdict
   → health → only-notable metric bullets → recommendations, with an explicit
   instruction to state only numbers present in the report.

This keeps weak models on rails: a single tool call, a small structured input,
and a constrained output shape.

## Error handling

- vROps credentials absent → existing credentials-error surfaced unchanged (the
  bot still runs).
- Partial failures (e.g. health succeeds but metric fetch fails) → report what
  succeeded, note the gap explicitly, never fabricate. Deterministic throughout.

## Testing (network-free, matching repo convention)

- `tests/test_analysis.py` — pure functions: trend (rising / falling / stable /
  single-point / empty series), threshold evaluation, each recommendation rule,
  verdict rollup, and edge cases (no samples, missing metrics).
- `vrops_diagnose` handler tests with a monkeypatched fake client covering:
  not-found, multiple-matches, healthy, critical, and partial-failure paths.

## Defaults to be chosen during implementation

- Standard metric set (above) and sensible default per-metric thresholds
  (e.g. CPU 90%, memory 90%, disk latency 20 ms) — implementer picks defaults,
  documented as constants.
- Alert/metric/recommendation caps.
