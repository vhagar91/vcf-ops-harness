"""In-memory conversation store.

Each Slack thread (or DM) has an ordered list of messages up to ``max_turns``.
"""

from __future__ import annotations

from ..config.types import Message


class ConversationMemory:
    """Thread-safe (asyncio) conversation history store."""

    def __init__(self, max_turns: int = 50) -> None:
        self._store: dict[str, list[Message]] = {}
        self._max_turns = max_turns

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _key(channel: str, thread_ts: str | None) -> str:
        return f"{channel}:{thread_ts}" if thread_ts else channel

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def append(
        self,
        channel: str,
        thread_ts: str | None,
        message: Message,
    ) -> None:
        k = self._key(channel, thread_ts)
        conv = self._store.setdefault(k, [])
        conv.append(message)

        if len(conv) > self._max_turns:
            self._store[k] = self._prune(conv)

    def _prune(self, conv: list[Message]) -> list[Message]:
        """Trim history to ``max_turns`` without orphaning tool messages.

        The kept window must begin at a ``user`` message so we never drop an
        assistant ``tool_calls`` message while keeping its ``tool`` replies
        (which the OpenAI API rejects with a 400).
        """
        system_msgs = [m for m in conv if m.role == "system"]
        non_system = [m for m in conv if m.role != "system"]

        keep = max(1, self._max_turns - len(system_msgs))
        window = non_system[-keep:]

        # Advance to the first clean turn boundary (a user message).
        for i, m in enumerate(window):
            if m.role == "user":
                window = window[i:]
                break
        else:
            window = []  # no user turn in window; drop dangling fragments

        return system_msgs + window

    def get_history(self, channel: str, thread_ts: str | None) -> list[Message]:
        return self._store.get(self._key(channel, thread_ts), [])

    def set_history(
        self,
        channel: str,
        thread_ts: str | None,
        messages: list[Message],
    ) -> None:
        self._store[self._key(channel, thread_ts)] = messages

    def clear(self, channel: str, thread_ts: str | None) -> None:
        self._store.pop(self._key(channel, thread_ts), None)

    @property
    def size(self) -> int:
        return len(self._store)