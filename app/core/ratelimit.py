"""Fixed-window rate limiting with in-memory and Redis backends.

What: ``RateLimiter`` interface; ``InMemoryRateLimiter`` (single process) and
``RedisRateLimiter`` (shared across API replicas), plus ``build_rate_limiter``.
Why: stops brute-force logins and runaway clients. Redis gives one shared counter when the
API is scaled horizontally; the in-memory fallback keeps local dev and tests dependency-free.
Why fixed window: simplest algorithm that is easy to explain; bursts of up to 2x the limit
at a window edge are an accepted trade-off (see docs/tradeoffs.md).
Connects to: ``core/security_middleware.py`` which applies it to incoming requests.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult: ...


class InMemoryRateLimiter:
    """Per-process counters. NOT shared between replicas (use Redis for that)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._windows: dict[str, tuple[float, int]] = {}  # key -> (window_end, count)

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = self._clock()
        self._evict_expired(now)
        window_end, count = self._windows.get(key, (0.0, 0))
        if window_end <= now:  # no window yet, or the previous one has finished: start fresh
            window_end, count = now + window_seconds, 0
        count += 1
        self._windows[key] = (window_end, count)
        return RateLimitResult(count <= limit, max(0, limit - count), max(1, int(window_end - now)))

    def _evict_expired(self, now: float) -> None:
        """Bound memory: drop finished windows once the table grows."""
        if len(self._windows) > 10_000:
            self._windows = {k: v for k, v in self._windows.items() if v[0] > now}


class RedisRateLimiter:
    """Shared counters using INCR + EXPIRE (atomic enough for fixed windows)."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        redis_key = f"rl:{key}"
        count = await self._client.incr(redis_key)
        if count == 1:
            await self._client.expire(redis_key, window_seconds)
        ttl = await self._client.ttl(redis_key)
        if ttl < 0:  # key lost its expiry (crash between INCR and EXPIRE): repair it
            await self._client.expire(redis_key, window_seconds)
            ttl = window_seconds
        return RateLimitResult(count <= limit, max(0, limit - count), max(1, ttl))


class FailOpenRateLimiter:
    """Wraps another limiter; if it errors (e.g. Redis down) allow the request.

    WHY fail open: a cache outage must not become a full API outage. The error is logged.
    """

    def __init__(self, inner: RateLimiter) -> None:
        self._inner = inner

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        try:
            return await self._inner.hit(key, limit, window_seconds)
        except Exception:
            logger.warning("rate limiter backend failed; allowing request", exc_info=True)
            return RateLimitResult(True, limit, window_seconds)


def build_rate_limiter(redis_url: str | None) -> RateLimiter:
    """Redis-backed when REDIS_URL is set, otherwise in-process."""
    if redis_url:
        return FailOpenRateLimiter(RedisRateLimiter(Redis.from_url(redis_url)))
    return InMemoryRateLimiter()
