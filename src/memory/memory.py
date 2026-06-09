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

        # Prune oldest non-system messages when over budget
        if len(conv) > self._max_turns:
            system_msgs = [m for m in conv if m.role == "system"]
            tail = conv[-(self._max_turns - len(system_msgs)) :]
            self._store[k] = system_msgs + tail

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