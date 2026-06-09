"""Slack bot integration using slack-bolt.

Listens for messages and feeds them into the pipeline.
"""

from __future__ import annotations

import re

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ..config.settings import HarnessConfig
from ..config.types import PipelineEvent
from ..memory.memory import ConversationMemory
from ..actions.registry import ActionRegistry
from ..pipeline.orchestrator import run_pipeline, PipelineMiddleware
from ..ai.llm import LlmConfig
from ..utils.logger import info, error


def create_and_start(config: HarnessConfig ,registry: ActionRegistry) -> None:
    """Create the Slack app, wire handlers, and start listening."""
    memory = ConversationMemory(max_turns=config.max_conversation_turns)

    if config.llm_provider == "ollama":
        llm_config = LlmConfig(
            api_key="ollama",  # Ollama doesn't need a real key
            model=config.ollama_model,
            base_url=config.ollama_base_url,
            system_prompt=config.system_prompt,
        )
    else:
        llm_config = LlmConfig(
            api_key=config.openai_api_key,
            model=config.openai_model,
            system_prompt=config.system_prompt,
        )

    # ------------------------------------------------------------------
    # Build Slack App
    # ------------------------------------------------------------------
    app = App(
        token=config.slack_bot_token,
        signing_secret=config.slack_signing_secret,
    )

    # ------------------------------------------------------------------
    # Shared message processing logic
    # ------------------------------------------------------------------
    def _process_text(
        text: str, user: str, channel: str, thread_ts: str | None, say: callable
    ) -> None:
        if not text:
            return
        if not user or user == "USLACKBOT":
            return
        if text.startswith("!"):
            return

        event = PipelineEvent(
            channel=channel,
            user_id=user,
            text=text,
            thread_ts=thread_ts,
        )

        try:
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                reply = loop.run_until_complete(
                    run_pipeline(event, memory, registry, llm_config)
                )
                if reply:
                    say(text=reply, thread_ts=thread_ts)
            finally:
                loop.close()
        except Exception as exc:
            error("Pipeline error", error=str(exc))
            say(text=f"⚠️ An error occurred: {exc}", thread_ts=thread_ts)

    # ------------------------------------------------------------------
    # Handle direct messages in a channel (no @-mention)
    # ------------------------------------------------------------------
    @app.message("")
    def handle_message(message: dict, say: callable) -> None:
        info("handle_message triggered", text=message.get("text", "")[:100])
        # Ignore bot messages
        if message.get("subtype") == "bot_message":
            info("Skipping bot_message")
            return
        _process_text(
            text=message.get("text", ""),
            user=message.get("user", ""),
            channel=message["channel"],
            thread_ts=message.get("thread_ts") or message.get("ts"),
            say=say,
        )

    # ------------------------------------------------------------------
    # Handle @-mentions (e.g. "@VCF Helper what time is it?")
    # ------------------------------------------------------------------
    @app.event("app_mention")
    def handle_mention(event: dict, say: callable) -> None:
        info("handle_mention triggered", text=event.get("text", "")[:100])
        # Strip the bot user ID mention (<@U12345>) from the text
        text = event.get("text", "")
        text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()

        _process_text(
            text=text,
            user=event.get("user", ""),
            channel=event["channel"],
            thread_ts=event.get("thread_ts") or event.get("ts"),
            say=say,
        )

    # ------------------------------------------------------------------
    # Slash command: /reset
    # ------------------------------------------------------------------
    @app.command("/reset")
    def reset_memory(ack: callable, command: dict, say: callable) -> None:
        ack()
        memory.clear(command["channel_id"], command.get("thread_ts"))
        say(text="🔄 Conversation memory cleared. Starting fresh!")

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------
    is_socket_mode = bool(config.slack_app_token)
    info(
        f"Slack bot configured (socketMode={is_socket_mode})",
        port=config.slack_port,
    )

    if is_socket_mode:
        info("Starting SocketModeHandler", app_token_prefix=config.slack_app_token[:12] + "...")
        handler = SocketModeHandler(
            app=app,
            app_token=config.slack_app_token,  # type: ignore[arg-type]
        )
        try:
            handler.start()
        except Exception as e:
            error("SocketModeHandler failed", error=str(e))
            raise
    else:
        info("Starting HTTP server")
        app.start(port=config.slack_port)
