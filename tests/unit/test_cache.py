"""Cache backends: TTL, LRU, atomic counter, graceful failure, hit/miss metrics."""

import fakeredis
import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.cache import InMemoryCache, MeteredCache, RedisCache, build_cache
from app.core.observability.metrics import REGISTRY


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_memory_cache_expires_entries() -> None:
    clock = Clock()
    cache = InMemoryCache(clock=clock)
    await cache.set("k", "v", ttl_seconds=10)
    assert await cache.get("k") == "v"
    clock.now += 10
    assert await cache.get("k") is None
    assert await cache.get("never-set") is None


async def test_memory_cache_evicts_the_least_recently_used() -> None:
    cache = InMemoryCache(max_entries=2)
    await cache.set("a", "1", 60)
    await cache.set("b", "2", 60)
    assert await cache.get("a") == "1"  # touching "a" makes "b" the oldest
    await cache.set("c", "3", 60)
    assert await cache.get("b") is None
    assert await cache.get("a") == "1" and await cache.get("c") == "3"


async def test_memory_cache_delete_and_counter() -> None:
    cache = InMemoryCache()
    await cache.set("k", "v", 60)
    await cache.delete("k")
    await cache.delete("k")  # deleting twice is fine
    assert await cache.get("k") is None
    assert [await cache.incr("n"), await cache.incr("n"), await cache.incr("n")] == [1, 2, 3]
    assert await cache.get("n") == "3"
    await cache.close()
    assert await cache.get("n") is None


async def test_redis_cache_round_trip() -> None:
    cache = RedisCache(fakeredis.FakeAsyncRedis(decode_responses=True))
    await cache.set("k", "v", 60)
    assert await cache.get("k") == "v"
    assert await cache.incr("n") == 1 and await cache.incr("n") == 2
    await cache.delete("k")
    assert await cache.get("k") is None
    await cache.close()


class BrokenRedis:
    async def get(self, *_: object, **__: object) -> None:
        raise RedisError("down")

    set = delete = incr = get


async def test_redis_failures_degrade_to_a_miss_instead_of_an_error() -> None:
    cache = RedisCache(BrokenRedis())  # type: ignore[arg-type]
    assert await cache.get("k") is None
    await cache.set("k", "v", 60)
    await cache.delete("k")
    assert await cache.incr("n") == 0


def hits(cache: str, result: str) -> float:
    return (
        REGISTRY.get_sample_value("cache_operations_total", {"cache": cache, "result": result})
        or 0.0
    )


async def test_metered_cache_counts_hits_and_misses() -> None:
    cache = MeteredCache(InMemoryCache(), "unit-test")
    before = (hits("unit-test", "hit"), hits("unit-test", "miss"))
    await cache.set("k", "v", 60)
    await cache.get("k")
    await cache.get("missing")
    await cache.get("missing")
    assert hits("unit-test", "hit") - before[0] == 1
    assert hits("unit-test", "miss") - before[1] == 2
    assert await cache.incr("n") == 1
    await cache.delete("k")
    await cache.close()


@pytest.mark.parametrize(
    ("url", "kind"), [(None, InMemoryCache), ("redis://localhost:1/0", RedisCache)]
)
def test_build_cache_picks_the_backend(url: str | None, kind: type) -> None:
    assert isinstance(build_cache(url, 10), kind)
    assert Redis  # imported to make the redis dependency explicit
