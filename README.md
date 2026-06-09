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
│   │   └── bot.py        # Slack bot (listener, message handler, /reset)
│   ├── ai/
│   │   └── llm.py        # LLM integration (OpenAI chat completions + tool calls)
│   ├── actions/
│   │   ├── registry.py   # Pluggable action registry
│   │   └── builtin/      # Built-in actions
│   │       ├── echo.py
│   │       └── get_time.py
│   ├── memory/
│   │   └── memory.py     # Per-thread conversation memory
│   ├── pipeline/
│   │   └── orchestrator.py  # Message processing pipeline (pre → LLM → post)
│   ├── utils/
│   │   ├── logger.py     # Structured logging
│   │   └── retry.py      # Exponential backoff retry
│   └── main.py           # Entry point
├── tests/
│   └── test_imports.py   # Import smoke test
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
| `OPENAI_API_KEY`         | ✅       | —         | OpenAI API key                       |
| `OPENAI_MODEL`           | ❌       | `gpt-4o`  | OpenAI model ID                      |
| `SYSTEM_PROMPT`          | ❌       | (default) | System prompt for the assistant      |
| `MAX_CONVERSATION_TURNS` | ❌       | `50`      | Max messages kept per conversation   |
| `LOG_LEVEL`              | ❌       | `INFO`    | `DEBUG`, `INFO`, `WARN`, `ERROR`     |

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