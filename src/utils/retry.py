"""Retry utility with exponential backoff for LLM / external API calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass
class RetryOptions:
    max_retries: int = 3
    base_delay_ms: float = 1_000.0
    max_delay_ms: float = 10_000.0


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    options: RetryOptions | None = None,
) -> T:
    """Execute *fn* and retry on any exception up to ``max_retries`` times."""
    opts = options or RetryOptions()
    last_err: BaseException | None = None

    for attempt in range(opts.max_retries + 1):
        try:
            return await fn()
        except BaseException as err:
            last_err = err
            if attempt == opts.max_retries:
                break
            delay = min(opts.base_delay_ms * (2**attempt), opts.max_delay_ms) / 1000
            await asyncio.sleep(delay)

    raise last_err  # type: ignore[misc]