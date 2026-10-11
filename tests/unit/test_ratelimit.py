"""Rate limiter backends behave identically; failures fail open."""

import fakeredis.aioredis
import pytest

from app.core.ratelimit import (
    FailOpenRateLimiter,
    InMemoryRateLimiter,
    RateLimitResult,
    RedisRateLimiter,
    build_rate_limiter,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_in_memory_blocks_after_limit_then_resets() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock)
    results = [await limiter.hit("k", 3, 60) for _ in range(5)]
    assert [r.allowed for r in results] == [True, True, True, False, False]
    assert results[2].remaining == 0 and results[3].retry_after_seconds >= 1
    clock.now += 61
    assert (await limiter.hit("k", 3, 60)).allowed


async def test_keys_are_independent() -> None:
    limiter = InMemoryRateLimiter()
    await limiter.hit("a", 1, 60)
    assert not (await limiter.hit("a", 1, 60)).allowed
    assert (await limiter.hit("b", 1, 60)).allowed


async def test_in_memory_evicts_expired_windows() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock)
    for i in range(10_001):
        await limiter.hit(f"k{i}", 5, 1)
    clock.now += 10
    await limiter.hit("fresh", 5, 1)
    assert len(limiter._windows) < 10_001


async def test_redis_backend_counts_and_expires() -> None:
    client = fakeredis.aioredis.FakeRedis()
    limiter = RedisRateLimiter(client)
    results = [await limiter.hit("ip1", 2, 60) for _ in range(3)]
    assert [r.allowed for r in results] == [True, True, False]
    assert 0 < await client.ttl("rl:ip1") <= 60


async def test_redis_backend_repairs_missing_expiry() -> None:
    client = fakeredis.aioredis.FakeRedis()
    await client.set("rl:x", 5)  # simulates crash between INCR and EXPIRE
    result = await RedisRateLimiter(client).hit("x", 100, 30)
    assert result.allowed and await client.ttl("rl:x") > 0


async def test_fail_open_when_backend_errors() -> None:
    class Broken:
        async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
            raise ConnectionError("redis down")

    result = await FailOpenRateLimiter(Broken()).hit("k", 1, 60)
    assert result.allowed


def test_builder_picks_backend() -> None:
    assert isinstance(build_rate_limiter(None), InMemoryRateLimiter)
    assert isinstance(build_rate_limiter("redis://localhost:6379/0"), FailOpenRateLimiter)


@pytest.mark.parametrize("limit", [1, 5])
async def test_remaining_never_negative(limit: int) -> None:
    limiter = InMemoryRateLimiter()
    for _ in range(limit + 3):
        result = await limiter.hit("k", limit, 60)
    assert result.remaining == 0
