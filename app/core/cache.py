"""Small async cache with two interchangeable backends: in-process TTL/LRU and Redis.

What: a ``Cache`` interface (get / set with TTL / delete / incr) plus ``InMemoryCache`` and
``RedisCache``; ``build_cache`` picks Redis when ``REDIS_URL`` is set, otherwise memory.
Why an interface: callers (retrieval, LLM client) never know which backend they have, so a
single API container works with zero infrastructure and several replicas share one Redis.
Why values are strings: the callers store JSON; it keeps both backends trivially compatible.
Why failures are swallowed: a cache is an optimisation. If Redis is down, a lookup is a miss and a
write is skipped - the request still succeeds, just slower. (Hits/misses/errors are counted.)
Connects to: ``services/knowledge.py`` (retrieval cache), ``llm/client.py`` (LLM cache).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.observability.metrics import CACHE_OPERATIONS

logger = logging.getLogger(__name__)


class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def incr(self, key: str) -> int:
        """Atomically add one (starting from 0) and return the new value. Never expires."""
        ...

    async def close(self) -> None: ...


class InMemoryCache:
    """Per-process TTL cache with LRU eviction (single event loop, so no locking needed)."""

    def __init__(
        self, max_entries: int = 1000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max = max_entries
        self._clock = clock
        self._data: OrderedDict[str, tuple[float | None, str]] = OrderedDict()

    async def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at is not None and expires_at <= self._clock():
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._data[key] = (self._clock() + ttl_seconds, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)  # least recently used

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str) -> int:
        current = self._data.get(key)
        value = int(current[1]) + 1 if current else 1
        self._data[key] = (None, str(value))
        return value

    async def close(self) -> None:
        self._data.clear()


class RedisCache:
    def __init__(self, redis: Redis, prefix: str = "cache:") -> None:
        self._redis = redis
        self._prefix = prefix

    async def get(self, key: str) -> str | None:
        try:
            value = await self._redis.get(self._prefix + key)
        except RedisError:
            logger.warning("redis cache get failed")
            return None
        return None if value is None else str(value)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        try:
            await self._redis.set(self._prefix + key, value, ex=ttl_seconds)
        except RedisError:
            logger.warning("redis cache set failed")

    async def delete(self, key: str) -> None:
        try:
            await self._redis.delete(self._prefix + key)
        except RedisError:
            logger.warning("redis cache delete failed")

    async def incr(self, key: str) -> int:
        try:
            return int(await self._redis.incr(self._prefix + key))
        except RedisError:
            logger.warning("redis cache incr failed")
            return 0

    async def close(self) -> None:
        await self._redis.aclose()


class MeteredCache:
    """Wraps a cache and counts hits and misses under a name (``retrieval``, ``llm``)."""

    def __init__(self, inner: Cache, name: str) -> None:
        self._inner = inner
        self._name = name

    async def get(self, key: str) -> str | None:
        value = await self._inner.get(key)
        CACHE_OPERATIONS.labels(self._name, "miss" if value is None else "hit").inc()
        return value

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._inner.set(key, value, ttl_seconds)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)

    async def incr(self, key: str) -> int:
        return await self._inner.incr(key)

    async def close(self) -> None:
        return None  # the shared backend is closed once by its owner


def build_cache(redis_url: str | None, max_entries: int) -> Cache:
    """Redis when configured, otherwise the in-process cache."""
    if redis_url:
        return RedisCache(Redis.from_url(redis_url, decode_responses=True))
    return InMemoryCache(max_entries)
