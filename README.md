# AI Harness — Slack Chat Bot

An extensible AI orchestration harness that runs as a **Slack chat bot**, processes natural language messages through an **LLM** (OpenAI), and can trigger **pluggable action plugins** to interact with infrastructure.

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Slack      │────▶│   Slack Bot      │────▶│   Pipeline      │
│   App        │     │   (slack-bolt)   │     │   (orchestrator) │
└──────────────┘     └──────────────────┘     └────────┬────────┘
                                                        │
              ┌─────────────────────────────────────────┼──────────┐
              │                                         │          │
              ▼                                         ▼          │
   ┌──────────────────┐                    ┌──────────────────┐    │
   │  Conversation    │                    │   AI / LLM       │    │
   │  Memory          │                    │   (OpenAI)       │    │
   └──────────────────┘                    └────────┬─────────┘    │
                                                     │             │
                                                     ▼             │
                                           ┌──────────────────┐    │
                                           │  Action Registry │◀───┘
                                           │  (pluggable)     │
                                           └────────┬─────────┘
                                                     │
                                           ┌─────────▼─────────┐
                                           │  Action Plugins   │
                                           │  (echo, get_time, │
                                           │   custom, ...)    │
                                           └───────────────────┘
```

## Directory Structure

```
harness/
├── src/
│   ├── config/           # Configuration loader & shared types
│   │   ├── settings.py   # load_config() from environment
│   │   └── types.py      # Domain types (Message, ActionDefinition, etc.)
│   ├── slack/
│   │   └── bot.py        # Slack bot (listener, off-thread dispatch, /reset, webhook start)
│   ├── ai/
│   │   ├── llm.py        # OpenAI/Ollama path (chat completions + tool calls)
│   │   └── anthropic_llm.py  # Native Anthropic/Claude path (same bounded loop)
│   ├── actions/
│   │   ├── registry.py   # Pluggable action registry
│   │   └── builtin/      # Built-in actions
│   │       ├── echo.py
│   │       ├── get_time.py
│   │       └── vrops/    # vROps client + tools
│   │           ├── vrops_client.py  # Authenticated REST client
│   │           ├── actions.py       # Read/admin action definitions
│   │           ├── analysis.py      # Pure scoring/trend/capacity/rightsizing helpers
│   │           ├── diagnose.py      # vrops_diagnose composite tool
│   │           ├── fleet.py         # Scope (site CHILD-walk) → bulk stats pipeline
│   │           ├── sites.py         # SiteMap: site → datacenter names
│   │           ├── reports.py       # Cluster/host capacity, oversized-VM, fleet_query tools
│   │           └── placement.py     # vrops_placement_recommendation
│   ├── webhook/          # Proactive alert notification (vROps → LLM → Slack)
│   │   ├── handler.py    # Pure request validation/parse
│   │   ├── server.py     # Embedded ThreadingHTTPServer (ack-fast + dispatch)
│   │   ├── alerts.py     # parse/enrich/prompt/process_alert
│   │   └── publisher.py  # Publisher protocol + SlackPublisher
│   ├── memory/
│   │   └── memory.py     # Per-thread conversation memory
│   ├── pipeline/
│   │   └── orchestrator.py  # Message processing pipeline (pre → LLM → post)
│   ├── utils/
│   │   ├── logger.py     # Structured logging
│   │   └── retry.py      # Exponential backoff retry
│   └── main.py           # Entry point
├── tests/                # pytest suite (robustness, alerts, fleet, placement, webhook, …)
├── docs/superpowers/     # Design specs + implementation plans
├── vrops-site-map.example.json  # Example site → datacenter map
├── .env.example          # Environment variable template
├── pyproject.toml
└── README.md
```

## Data Flow

1. **Slack message** arrives → bot handler in `slack/bot.py`.
2. **Pipeline** (`pipeline/orchestrator.py`) wraps the event with optional pre/post middleware.
3. **LLM** (`ai/llm.py`) appends the user message to `ConversationMemory`, then calls OpenAI with the full history + registered tool definitions.
4. If the LLM requests a **tool call**, the `ActionRegistry` executes the named action and feeds the result back to the LLM.
5. The final natural-language response is sent back to the Slack thread.

## Adding a New Action

Actions are self-contained plugins. Create a new file in `src/actions/builtin/` (or a new subpackage) that exports an `ActionDefinition`:

```python
from config.types import ActionDefinition, ActionResult

async def my_handler(args: dict) -> ActionResult:
    # Your logic here
    return ActionResult(success=True, summary="Done!")

my_action = ActionDefinition(
    name="my_action",
    description="Does something useful.",
    input_schema={
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "A parameter"},
        },
        "required": ["param1"],
    },
    handler=my_handler,
)
```

Then register it in `src/main.py`:

```python
registry.register(my_action)
```

## Environment Variables

| Variable                 | Required | Default   | Description                          |
|--------------------------|----------|-----------|--------------------------------------|
| `SLACK_BOT_TOKEN`        | ✅       | —         | Slack bot token (`xoxb-*`)           |
| `SLACK_SIGNING_SECRET`   | ✅       | —         | Slack signing secret                 |
| `SLACK_APP_TOKEN`        | ❌       | —         | Socket mode token (`xapp-*`)         |
| `SLACK_PORT`             | ❌       | `3000`    | Port for HTTP mode                   |
| `LLM_PROVIDER`           | ❌       | `openai`  | `openai`, `ollama`, or `anthropic`   |
| `OPENAI_API_KEY`         | ✅*      | —         | OpenAI API key (*if provider=openai) |
| `OPENAI_MODEL`           | ❌       | `gpt-4o`  | OpenAI model ID                      |
| `ANTHROPIC_API_KEY`      | ✅*      | —         | Anthropic API key (*if provider=anthropic) |
| `ANTHROPIC_MODEL`        | ❌       | `claude-opus-4-8` | Claude model ID              |
| `SYSTEM_PROMPT`          | ❌       | (grounding default) | System prompt for the assistant |
| `MAX_CONVERSATION_TURNS` | ❌       | `50`      | Max messages kept per conversation   |
| `MAX_OUTPUT_TOKENS`      | ❌       | `800`     | Max tokens generated per reply       |
| `REQUEST_TIMEOUT_S`      | ❌       | `60`      | Per-request LLM timeout (seconds)    |
| `MAX_TOOL_ITERATIONS`    | ❌       | `5`       | Max tool-call rounds per message     |
| `IS_THINKING_MODEL`      | ❌       | `auto`    | `auto`/`true`/`false` — strip `<think>` blocks (qwen3) |
| `VROPS_SERVER`           | ❌       | —         | vROps server FQDN/IP (for vROps tools) |
| `VROPS_USERNAME`         | ❌       | —         | vROps username                       |
| `VROPS_PASSWORD`         | ❌       | —         | vROps password                       |
| `VROPS_AUTH_SOURCE`      | ❌       | `Local`   | vROps auth source                    |
| `VROPS_SITE_MAP_FILE`    | ❌       | —         | JSON file mapping site → datacenter names, for location-scoped fleet queries (see `vrops-site-map.example.json`) |
| `WEBHOOK_ENABLED`        | ❌       | `false`   | Enable the inbound vROps alert webhook listener (proactive notifications) |
| `WEBHOOK_PORT`           | ❌       | `8088`    | Port for the webhook listener        |
| `WEBHOOK_TOKEN`          | ❌       | —         | Shared secret on inbound webhooks (header `X-Webhook-Token` or `?token=`); required when enabled |
| `WEBHOOK_PATH`           | ❌       | `/vrops/alert` | Accepted webhook POST path      |
| `VROPS_ALERT_CHANNEL`    | ❌       | —         | Slack channel to publish alert summaries to; required when webhook enabled |
| `WEBHOOK_MIN_CRITICALITY`| ❌       | —         | Optional floor: `INFORMATION`/`WARNING`/`IMMEDIATE`/`CRITICAL` (empty = all) |
| `LOG_LEVEL`              | ❌       | `INFO`    | `DEBUG`, `INFO`, `WARN`, `ERROR`     |

## LLM providers

The harness supports three providers, selected via `LLM_PROVIDER`:

| Provider | `LLM_PROVIDER` | SDK | Notes |
|----------|----------------|-----|-------|
| OpenAI   | `openai`       | `openai` | e.g. `gpt-4o` |
| Ollama (local) | `ollama` | `openai` (compat endpoint) | e.g. `qwen3:4b`; thinking auto-disabled |
| Anthropic / Claude | `anthropic` | `anthropic` (native) | e.g. `claude-opus-4-8` |

Claude uses the **native Anthropic SDK** (`src/ai/anthropic_llm.py`) — system
prompt as a top-level param, tools via `input_schema`, content-block responses —
not an OpenAI-compatibility shim. The same bounded loop, grounding prompt, and
token guardrails apply to all three.

## How responses are generated

Each user message runs a **bounded agentic loop** (up to `MAX_TOOL_ITERATIONS`
rounds): the model may chain tool calls — e.g. `vrops_search_resources` → get an
ID → `vrops_get_latest_stats` — before producing a final, grounded answer. Tool
output is size-capped before re-entering the context, replies are capped at
`MAX_OUTPUT_TOKENS`, and `<think>` blocks from thinking models are stripped. The
default system prompt instructs the model to answer **only** from tool data.

The pipeline runs **off the Slack event-listener thread** (a daemon thread per
message): Socket Mode must ack the event envelope within ~3 s, but a slow model can
take far longer, so the handler returns immediately (after the "🔎 Working on it…"
ack) and posts the reply when ready. Running it inline previously caused Slack to
drop and churn connections (`ConnectionResetError`).

## vROps tools

Read (health / alerts / performance):

| Tool | Purpose |
|------|---------|
| `vrops_search_resources`     | Find resources by name; returns all matches + IDs + health |
| `vrops_get_resource_health`  | Health (GREEN/YELLOW/ORANGE/RED) + status states |
| `vrops_get_alerts`           | Active alerts, filterable by resource and criticality |
| `vrops_get_alert`            | Full detail for one alert |
| `vrops_get_stat_keys`        | Discover available metric keys for a resource |
| `vrops_get_latest_stats`     | Most recent metric values ("current CPU usage") |
| `vrops_get_stats`            | Time-series summary (count/latest/min/max/avg) over a window |

Plus the original write/admin tools: `vrops_find_resource`,
`vrops_get_resource_properties`, `vrops_create_resource`, `vrops_push_properties`,
`vrops_push_event`, `vrops_get_monitored_vcenters`,
`vrops_get_monitored_nsxt_managers`, `vrops_add_child_relationship`,
`vrops_get_version`.

### Operations assistant (composite + fleet tools)

These do the heavy lifting in Python and return one compact, already-ranked report,
so the model makes a single tool call and only narrates the result:

| Tool | Purpose |
|------|---------|
| `vrops_diagnose` | One-call triage of a resource: health + active alerts + metric trends + ranked recommendations |
| `vrops_cluster_capacity_report` | Rank clusters by free capacity **by type (cpu/memory/storage)**, naming the bottleneck; optional `location` |
| `vrops_host_capacity_report` | Same, for ESXi hosts (`HostSystem`) |
| `vrops_oversized_vms_report` | Oversized VMs ranked by reclaimable vCPU/memory (vROps native rightsizing) |
| `vrops_fleet_query` | Generic "rank all X by metric Y" across a kind, optionally scoped to a site |
| `vrops_placement_recommendation` | Best host to place a new VM of a given `vcpu`/`memory_gb`, ranked by free headroom after placement; names the blocker when nothing fits |

**Site scoping.** Fleet/placement tools accept a `location` (e.g. `Madrid`, `lab`). A
`SiteMap` loaded from `VROPS_SITE_MAP_FILE` maps each site to its vROps **Datacenter**
names; resources are scoped by a recursive single-hop `CHILD` walk (vROps relationships
have no transitive `DESCENDANT`). Capacity is reported both as the vROps capacity-engine
view (post-HA/buffer) and raw headroom; placement fits/ranks on raw headroom and surfaces
an HA-reservation caveat. See `vrops-site-map.example.json`.

## Proactive alert notification (vROps webhook → LLM → Slack)

When enabled (`WEBHOOK_ENABLED=true`), the bot runs an embedded HTTP listener in the same
process, alongside the Slack connection. Point a vROps **Webhook Outbound** notification at
`http://<bot-host>:<WEBHOOK_PORT><WEBHOOK_PATH>` with header `X-Webhook-Token: <WEBHOOK_TOKEN>`.

On each alert the bot: validates the token, **acks `202` immediately**, then (in a
background thread) resolves the resource name + alert detail, runs the alert through the
agentic pipeline for an executive summary + 3 remediation steps, and publishes the result
to `VROPS_ALERT_CHANNEL`. If the LLM fails, it still posts a fallback with the raw alert so
nothing is silently dropped. The listener is **off by default** and refuses to start without
both a token (else it would be an open endpoint) and an alert channel. Output goes through a
small `Publisher` seam (`src/webhook/publisher.py`) so Teams/ticketing can be added later.

## Running

```bash
cd harness
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env   # edit with real tokens
python3 -m src.main
```

## Slack Bot Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → Create New App.
2. Enable **Socket Mode** (optional, but simpler for development).
3. Add the **`chat:write`**, **`app_mentions:read`**, **`channels:history`**, and **`commands`** OAuth scopes.
4. Install the app to your workspace.
5. Copy the **Bot Token** (`xoxb-*`), **Signing Secret**, and optionally the **App-Level Token** (`xapp-*`) into `.env`.

### Slash Commands

| Command    | Description                          |
|------------|--------------------------------------|
| `/reset`   | Clears the conversation memory for the current channel/thread. |

## Testing

```bash
cd harness
.venv/bin/python -m pytest tests/ -v