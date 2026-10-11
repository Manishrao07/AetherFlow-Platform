"""Exponential backoff with full jitter - generic, used for LLM calls and tool steps.

What: ``RetryPolicy`` plus ``retry_async`` which re-runs an async function while the error is
retryable, sleeping a growing, randomised delay between attempts.
Why exponential: gives a struggling service room to recover. Why *jitter*: if 100 clients all
retry at exactly 1s, 2s, 4s they hammer the server in synchronised waves ("thundering herd");
random delays spread them out. We use "full jitter" (delay = random(0, cap)).
Why honour Retry-After: when a server says "come back in 7s", obeying beats guessing.
Connects to: ``client.py`` (LLM), orchestrator step retries (Phase 5).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_delay < 0 or self.max_delay < self.base_delay:
            raise ValueError("invalid retry policy")


def backoff_delay(
    attempt: int,
    policy: RetryPolicy,
    rng: random.Random | None = None,
    retry_after: float | None = None,
) -> float:
    """Seconds to wait after failed attempt number ``attempt`` (1-based)."""
    if retry_after is not None:
        return min(max(retry_after, 0.0), policy.max_delay)
    cap = min(policy.max_delay, policy.base_delay * (2 ** (attempt - 1)))
    return (rng or random).uniform(0, cap)


async def retry_async(
    func: Callable[[int], Awaitable[T]],
    policy: RetryPolicy,
    *,
    is_retryable: Callable[[BaseException], bool],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call ``func(attempt)`` until it succeeds, a non-retryable error occurs, or attempts run out.

    The last error is re-raised unchanged so callers keep the original type.
    """
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await func(attempt)
        except Exception as exc:
            if attempt >= policy.max_attempts or not is_retryable(exc):
                raise
            delay = backoff_delay(attempt, policy, rng, getattr(exc, "retry_after", None))
            logger.warning(
                "retrying after error",
                extra={"attempt": attempt, "delay_s": round(delay, 3), "error": type(exc).__name__},
            )
            if on_retry:
                on_retry(attempt, exc, delay)
            await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover  (loop always returns or raises)
