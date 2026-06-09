"""Retry utility with exponential backoff for LLM / external API calls."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from .logger import warn

T = TypeVar("T")

# HTTP statuses where retrying won't help (client errors). 408/429 are excluded
# on purpose — those are transient and worth retrying.
_NON_RETRYABLE_STATUS = {400, 401, 403, 404, 405, 409, 422}


@dataclass
class RetryOptions:
    max_retries: int = 3
    base_delay_ms: float = 1_000.0
    max_delay_ms: float = 10_000.0


def _is_retryable(err: BaseException) -> bool:
    """Decide whether retrying *err* could plausibly succeed."""
    # Programming / parsing errors: never retry.
    if isinstance(err, (json.JSONDecodeError, TypeError, ValueError, KeyError)):
        return False
    # HTTP-style errors expose a status code (e.g. openai.APIStatusError).
    status = getattr(err, "status_code", None) or getattr(err, "status", None)
    if isinstance(status, int) and status in _NON_RETRYABLE_STATUS:
        return False
    return True


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    options: RetryOptions | None = None,
) -> T:
    """Execute *fn*, retrying transient failures with exponential backoff.

    Non-retryable errors (4xx client errors, parse errors) are raised
    immediately instead of being retried pointlessly.
    """
    opts = options or RetryOptions()
    last_err: BaseException | None = None

    for attempt in range(opts.max_retries + 1):
        try:
            return await fn()
        except BaseException as err:
            last_err = err
            if attempt == opts.max_retries or not _is_retryable(err):
                if not _is_retryable(err):
                    warn("Non-retryable error; not retrying", error=str(err))
                break
            delay = min(opts.base_delay_ms * (2**attempt), opts.max_delay_ms) / 1000
            await asyncio.sleep(delay)

    raise last_err  # type: ignore[misc]