# Proactive Alert Notification (vROps Webhook → LLM → Slack)

**Date:** 2026-06-12
**Status:** Design approved, pending spec review
**Branch:** continues on `feat/ops-assistant-fleet-queries` (or a follow-up branch)

## Goal

When vROps raises an alert, its **Webhook Outbound Plugin** POSTs the alert JSON to this
bot. The bot enriches it, asks the LLM for an executive summary + step-by-step remediation,
and publishes the result to Slack for operators.

> "Analiza este error de vSphere y dame 3 pasos para solucionarlo."

## Decisions (confirmed with user)

1. **Middleware is embedded in the bot process** — an inbound HTTP listener runs in a
   daemon thread alongside the existing Slack Socket Mode connection. No separate service.
2. **LLM path: reuse the agentic pipeline** — the alert becomes a synthetic message fed to
   `run_pipeline`, so the model can call vROps tools to investigate beyond the enrichment.
3. **Enrichment: light** — resolve `resourceId → name` and fetch the alert's full detail
   before building the prompt.
4. **Output: Slack, via a pluggable publisher** — post to a configured channel; a small
   `Publisher` seam lets Teams/ticketing be added later without touching the core.

## Architecture

New package `src/webhook/`:

```
src/webhook/
  __init__.py
  server.py       # ThreadingHTTPServer + BaseHTTPRequestHandler (thin glue) + start_webhook_server()
  handler.py      # PURE handle_webhook(method, path, headers, body, *, token, expected_path) -> WebhookDecision
  alerts.py       # AlertInfo, parse_alert, enrich, build_prompt, process_alert
  publisher.py    # Publisher protocol + SlackPublisher
```

### Data flow

1. vROps `POST /vrops/alert` with the alert JSON.
2. `handle_webhook` (pure) validates method/path/token/size/JSON → returns a `WebhookDecision`
   (HTTP status + parsed payload when accepted). The request handler sends the status and,
   on accept, **returns `202` immediately** and dispatches `process_alert` to a background
   daemon thread (ack-fast: vROps webhooks time out quickly; the pipeline takes minutes).
3. `process_alert(payload, client, memory, registry, llm_config, publisher, min_criticality)`:
   - `parse_alert(payload) -> AlertInfo`
   - optional criticality filter (drop below `min_criticality` when configured)
   - `enrich(client, alert)` → `{resource_name, alert_detail}`
   - `build_prompt(alert, context)` → message text
   - `run_pipeline(synthetic_event, memory, registry, llm_config)` → remediation text
   - `publisher.publish(title, body)`
   - On any processing error: publish a **fallback** (raw alert headline + "summary
     unavailable") so the alert is never silently dropped.

### Synthetic pipeline event

`PipelineEvent(channel="vrops-webhook", user_id="vrops", text=<built prompt>, thread_ts=<alertId>)`.
Using `alertId` as the thread key isolates each alert in `ConversationMemory` (no collision
with user chats, no stale context carried between alerts).

### `AlertInfo` (parsed, tolerant)

vROps payload templates are user-defined, so `parse_alert` reads common keys with fallbacks
and keeps the raw payload:

```python
@dataclass
class AlertInfo:
    alert_id: str | None        # alertId / id
    name: str | None            # alertName / alertDefinitionName / name
    criticality: str | None     # criticality / alertLevel / status (normalized upper)
    status: str | None
    resource_id: str | None     # resourceId / entityId
    resource_name: str | None   # resourceName (if present in payload)
    start_time: int | None
    raw: dict                   # the full payload, always passed to the prompt
```

### `build_prompt`

Embeds the enriched facts and a fixed instruction, e.g.:

```
A vROps alert fired. Analyze it and respond for on-call operators with:
1) a one-line executive summary, 2) the affected object, 3) exactly 3 concrete
remediation steps. Be specific and technical; do not invent values.

Alert: <name> (criticality <CRIT>) on <resource_name> [<resource_kind>]
Status: <status>  Started: <start_time>
Alert detail: <key fields from get_alert>
Raw payload: <compact JSON>
```

## Configuration (new `HarnessConfig` fields + `.env` / `.env.example`)

| Var | Default | Meaning |
|-----|---------|---------|
| `WEBHOOK_ENABLED` | `false` | Start the inbound listener. Off → bot behaves exactly as today. |
| `WEBHOOK_PORT` | `8088` | Listener port (separate from `SLACK_PORT`). |
| `WEBHOOK_TOKEN` | `""` | Shared secret. **If enabled but unset, the listener refuses to start.** |
| `WEBHOOK_PATH` | `/vrops/alert` | Accepted POST path. |
| `VROPS_ALERT_CHANNEL` | `""` | Slack channel id/name to publish to. Required when enabled. |
| `WEBHOOK_MIN_CRITICALITY` | `""` | Optional floor (e.g. `CRITICAL`); empty = act on all. |

`start_webhook_server` is only called from `create_and_start` when `WEBHOOK_ENABLED` is true,
`WEBHOOK_TOKEN` is set, and `VROPS_ALERT_CHANNEL` is set; otherwise it logs why and skips
(fail-safe — never an unauthenticated endpoint, never a listener that can't publish).

## Security

- Binds `0.0.0.0` so vROps can reach it; the token gates every request.
- Token accepted via `X-Webhook-Token` header **or** `?token=` query param (some vROps
  webhook configs can't set custom headers).
- Mismatch/missing token → `401`; wrong path → `404`; non-POST → `405`; body over a size
  cap (e.g. 1 MB) → `413`; malformed JSON → `400`.
- Secrets are never written to logs.

## Error handling

- The `202` ack is independent of processing — vROps never blocks on the LLM.
- Background worker wraps enrich/pipeline/publish in try/except; failures are logged and a
  minimal fallback message (raw alert headline) is published so notifications aren't lost.
- Missing alert id / unparseable payload → still `400` at the HTTP layer (rejected before
  dispatch).

## Testing (pure / no network, matching house style)

- `handle_webhook`: valid token (header and query) → accept/`202` + payload; bad/missing
  token → `401`; wrong path → `404`; non-POST → `405`; bad JSON → `400`; oversized → `413`.
- `parse_alert`: multiple vROps payload shapes (templated, header-style) → correct
  `AlertInfo`; missing fields tolerated; raw retained.
- `build_prompt`: contains resource name, criticality, and the 3-step instruction.
- `enrich`: fake `VropsClient` (`get_resource_names`, `get_alert`) → expected context.
- `process_alert`: fake client + monkeypatched `run_pipeline` + fake publisher → publishes
  the pipeline output; when `run_pipeline` raises → publishes the fallback; criticality
  filter drops sub-threshold alerts (no publish).
- `SlackPublisher.publish`: fake `app.client` → `chat_postMessage(channel=..., text=...)`.
- The `ThreadingHTTPServer` wiring is not unit-tested (thin glue, consistent with the Slack
  and `vrops_client` HTTP code); verified by a localhost `curl` smoke.

## Out of scope (YAGNI)

- Teams / ticketing publishers (the `Publisher` seam is built; concrete adapters are later).
- Dedup / alert-storm throttling beyond the optional criticality filter.
- Persistence / retry queue for failed deliveries.
- Any write-back to vROps (enrichment is read-only).
- Multiple alerts per request / batch payloads (one alert per POST).
