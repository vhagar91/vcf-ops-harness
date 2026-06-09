"""Message processing pipeline.

Each incoming Slack message flows through:
  1. Pre-processing (validation, filtering)
  2. LLM inference (with action resolution)
  3. Post-processing (rate limiting, logging)
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Callable

from ..config.types import PipelineEvent
from ..memory.memory import ConversationMemory
from ..actions.registry import ActionRegistry
from ..ai.llm import process_with_llm, LlmConfig
from ..utils.logger import info


class PipelineMiddleware:
    """Optional hooks called before / after LLM processing."""

    pre_process: Callable[[PipelineEvent], Awaitable[bool]] | None = None
    post_process: Callable[[PipelineEvent, str], Awaitable[None]] | None = None


async def run_pipeline(
    event: PipelineEvent,
    memory: ConversationMemory,
    registry: ActionRegistry,
    llm_config: LlmConfig,
    middleware: PipelineMiddleware | None = None,
) -> str | None:
    """Run the full message processing pipeline.

    Returns the reply text, or ``None`` if the message was skipped.
    """
    # 1. Pre-processing
    if middleware and middleware.pre_process:
        should_continue = await middleware.pre_process(event)
        if not should_continue:
            info("Message skipped by pre-process middleware", user_id=event.user_id)
            return None

    # 2. Run LLM + action loop
    info(
        "Processing message",
        user_id=event.user_id,
        channel=event.channel,
        text=event.text[:120],
    )

    reply = await process_with_llm(
        user_message=event.text,
        channel=event.channel,
        thread_ts=event.thread_ts,
        memory=memory,
        registry=registry,
        config=llm_config,
    )

    # 3. Post-processing
    if middleware and middleware.post_process:
        await middleware.post_process(event, reply)

    return reply