"""Output publishers for proactive notifications. v1 posts to Slack; the Publisher
protocol lets Teams/ticketing adapters be added without touching the alert core."""

from __future__ import annotations

from typing import Protocol

from ..utils.logger import error


class Publisher(Protocol):
    def publish(self, title: str, body: str) -> None: ...


class SlackPublisher:
    """Posts to a Slack channel via the bolt App's WebClient (`app.client`)."""

    def __init__(self, client, channel: str):
        self._client = client
        self._channel = channel

    def publish(self, title: str, body: str) -> None:
        text = f"*{title}*\n{body}" if title else body
        try:
            self._client.chat_postMessage(channel=self._channel, text=text)
        except Exception as e:
            error("Failed to publish Slack notification", error=str(e))
