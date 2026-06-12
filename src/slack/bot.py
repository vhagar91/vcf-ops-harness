"""Slack bot integration using slack-bolt.

Listens for messages and feeds them into the pipeline.
"""

from __future__ import annotations

import asyncio
import re
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ..config.settings import HarnessConfig
from ..config.types import PipelineEvent
from ..memory.memory import ConversationMemory
from ..actions.registry import ActionRegistry
from ..pipeline.orchestrator import run_pipeline, PipelineMiddleware
from ..ai.llm import LlmConfig
from ..utils.logger import info, error
from ..webhook.server import start_webhook_server
from ..webhook.publisher import SlackPublisher
from ..webhook.alerts import process_alert
from ..actions.builtin.vrops.actions import _build_client


def _run_pipeline_in_thread(event, thread_ts, say, memory, registry, llm_config) -> None:
    """Run the (potentially minutes-long) agentic pipeline OFF the Slack event-listener
    thread, then post the reply.

    Slack's Socket Mode requires the event envelope to be acked within ~3 seconds, but
    slack_bolt's SocketModeHandler only acks AFTER the listener returns. Running the
    pipeline inline therefore blocked the ack for minutes (qwen3:4b is slow), so Slack
    retried the event and reset the websocket (ConnectionResetError, rotating session
    ids). Dispatching to a daemon thread lets the listener return immediately so the
    envelope is acked on time; the reply is posted asynchronously when ready.
    """

    def _worker() -> None:
        try:
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
            error("Pipeline error", error=str(exc), type=type(exc).__name__)
            if "connection" in str(exc).lower():
                msg = (
                    "⚠️ Couldn't reach the LLM backend. Check that the model "
                    "server (Ollama / OpenAI) is running and reachable, then retry."
                )
            else:
                msg = f"⚠️ An error occurred: {exc}"
            try:
                say(text=msg, thread_ts=thread_ts)
            except Exception:
                pass  # reply is best-effort

    threading.Thread(target=_worker, name="vrops-pipeline", daemon=True).start()


def _maybe_start_webhook(app, config, memory, registry, llm_config) -> None:
    """Start the proactive vROps alert webhook listener, if configured. Fail-safe:
    never starts without a token (would be an open endpoint) or an alert channel
    (nowhere to publish)."""
    if not config.webhook_enabled:
        return
    if not config.webhook_token:
        error("WEBHOOK_ENABLED but WEBHOOK_TOKEN is empty; not starting webhook listener")
        return
    if not config.vrops_alert_channel:
        error("WEBHOOK_ENABLED but VROPS_ALERT_CHANNEL is empty; not starting webhook listener")
        return

    # app.client is a WebClient (bot-token Web API) independent of the Socket Mode /
    # HTTP server loop, so publishing works even before/while Slack is connecting —
    # which is why starting this listener before the blocking Slack start is safe.
    publisher = SlackPublisher(app.client, config.vrops_alert_channel)

    def _dispatch(payload):
        try:
            client = _build_client({})
        except Exception:
            client = None  # no vROps creds -> summarize from the raw payload only
        process_alert(payload, client, memory, registry, llm_config, publisher,
                      config.webhook_min_criticality)

    start_webhook_server(config.webhook_port, config.webhook_token,
                         config.webhook_path, _dispatch)


def create_and_start(config: HarnessConfig ,registry: ActionRegistry) -> None:
    """Create the Slack app, wire handlers, and start listening."""
    memory = ConversationMemory(max_turns=config.max_conversation_turns)

    _common = dict(
        provider=config.llm_provider,
        system_prompt=config.system_prompt,
        is_thinking_model=config.is_thinking_model,
        max_output_tokens=config.max_output_tokens,
        request_timeout_s=config.request_timeout_s,
        max_tool_iterations=config.max_tool_iterations,
    )
    if config.llm_provider == "ollama":
        llm_config = LlmConfig(
            api_key="ollama",  # Ollama doesn't need a real key
            model=config.ollama_model,
            base_url=config.ollama_base_url,
            **_common,
        )
    elif config.llm_provider == "anthropic":
        llm_config = LlmConfig(
            api_key=config.anthropic_api_key,
            model=config.anthropic_model,
            **_common,
        )
    else:
        llm_config = LlmConfig(
            api_key=config.openai_api_key,
            model=config.openai_model,
            **_common,
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

        # Quick ack so a multi-step (and now slower) call doesn't look like a hang.
        # NB: this is a Web API post, NOT the Socket Mode envelope ack — the envelope
        # is acked by the listener returning, which is why the pipeline must not run
        # inline here. See _run_pipeline_in_thread.
        try:
            say(text="🔎 Working on it…", thread_ts=thread_ts)
        except Exception:
            pass  # ack is best-effort

        # Run the agentic loop off the listener thread so the envelope is acked on
        # time; the reply is posted asynchronously when it completes.
        _run_pipeline_in_thread(event, thread_ts, say, memory, registry, llm_config)

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
    # Optional: proactive vROps alert webhook (embedded listener).
    _maybe_start_webhook(app, config, memory, registry, llm_config)

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
