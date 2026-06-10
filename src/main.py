"""AI Harness — Main entry point.

Usage:
    export SLACK_BOT_TOKEN=xoxb-...
    export SLACK_SIGNING_SECRET=...
    export OPENAI_API_KEY=sk-...
    python -m src.main
"""

from __future__ import annotations

import os
import sys
from dotenv import load_dotenv

from .config.settings import load_config
from .actions.registry import ActionRegistry
from .actions.builtin.echo import echo_action
from .actions.builtin.get_time import get_time_action
from .actions.builtin.vrops.actions import vrops_actions
from .actions.builtin.vrops.diagnose import vrops_diagnose_action
from .slack.bot import create_and_start
from .utils.logger import info, error, set_log_level, LogLevel


def main() -> None:
    load_dotenv()

    # Log level from env
    level = os.environ.get("LOG_LEVEL", "INFO")
    try:
        set_log_level(LogLevel(level.upper()))
    except ValueError:
        pass  # fall back to INFO

    # 1. Load config
    config = load_config()
    provider = config.llm_provider
    active_model = config.ollama_model if provider == "ollama" else config.openai_model
    info(
        "Configuration loaded",
        slack_port=config.slack_port,
        provider=provider,
        model=active_model,
        socket_mode=bool(config.slack_app_token),
    )

    # 2. Create action registry and register built-in actions
    registry = ActionRegistry()
    registry.register(echo_action)
    registry.register(get_time_action)

    # Register vROps actions
    for action in vrops_actions:
        registry.register(action)
    registry.register(vrops_diagnose_action)

    # 3. Build and start Slack bot
    create_and_start(config,registry)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error("Fatal startup error", error=str(exc))
        sys.exit(1)