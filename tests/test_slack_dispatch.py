"""The Slack pipeline must run OFF the event-listener thread so Socket Mode can
ack the event envelope within Slack's 3s window (otherwise Slack retries the event
and resets the websocket — ConnectionResetError across rotating session ids)."""

from __future__ import annotations

import threading
import time

from src.slack import bot
from src.config.types import PipelineEvent


def _say_recorder():
    replies = []

    def say(text, thread_ts=None):
        replies.append(text)

    return replies, say


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_dispatch_returns_immediately_and_replies_async(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    async def slow_pipeline(event, memory, registry, llm_config):
        started.set()
        release.wait(timeout=5)  # simulate a minutes-long agentic loop
        return "done"

    monkeypatch.setattr(bot, "run_pipeline", slow_pipeline)
    replies, say = _say_recorder()
    event = PipelineEvent(channel="C", user_id="U", text="hi", thread_ts=None)

    t0 = time.monotonic()
    bot._run_pipeline_in_thread(event, None, say, memory=None, registry=None, llm_config=None)
    elapsed = time.monotonic() - t0

    # The dispatch must NOT block on the pipeline (that's what delays the envelope ack).
    assert elapsed < 0.5
    assert started.wait(timeout=2)  # worker really started
    assert replies == []            # nothing posted yet — pipeline still running

    release.set()
    assert _wait_for(lambda: replies == ["done"])


def test_dispatch_reports_pipeline_error(monkeypatch):
    async def boom(event, memory, registry, llm_config):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(bot, "run_pipeline", boom)
    replies, say = _say_recorder()
    event = PipelineEvent(channel="C", user_id="U", text="hi", thread_ts=None)

    bot._run_pipeline_in_thread(event, None, say, memory=None, registry=None, llm_config=None)

    assert _wait_for(lambda: bool(replies))
    assert "error" in replies[0].lower() or "couldn't reach" in replies[0].lower()
