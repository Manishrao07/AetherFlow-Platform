"""Circuit breaker state machine, driven by a fake clock."""

import asyncio

import pytest

from app.core.llm.base import CircuitOpenError, LLMBadRequestError, LLMServerError
from app.core.llm.circuit_breaker import BreakerState, CircuitBreaker


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def ok() -> str:
    return "ok"


async def boom() -> str:
    raise LLMServerError("down")


def make(clock: Clock, **kwargs: object) -> CircuitBreaker:
    return CircuitBreaker("t", failure_threshold=3, recovery_seconds=10, clock=clock, **kwargs)  # type: ignore[arg-type]


async def trip(breaker: CircuitBreaker) -> None:
    for _ in range(3):
        with pytest.raises(LLMServerError):
            await breaker.call(boom)


async def test_opens_after_threshold_then_fails_fast() -> None:
    breaker = make(Clock())
    await trip(breaker)
    assert breaker.state is BreakerState.OPEN and breaker.is_open
    with pytest.raises(CircuitOpenError):
        await breaker.call(ok)  # not even attempted


async def test_success_resets_failure_count() -> None:
    breaker = make(Clock())
    for _ in range(2):
        with pytest.raises(LLMServerError):
            await breaker.call(boom)
    assert await breaker.call(ok) == "ok"
    for _ in range(2):  # two more failures: still below threshold because the count was reset
        with pytest.raises(LLMServerError):
            await breaker.call(boom)
    assert breaker.state is BreakerState.CLOSED


async def test_half_open_probe_success_closes() -> None:
    clock = Clock()
    breaker = make(clock)
    await trip(breaker)
    clock.now += 11
    assert breaker.state is BreakerState.HALF_OPEN
    assert await breaker.call(ok) == "ok"
    assert breaker.state is BreakerState.CLOSED


async def test_half_open_probe_failure_reopens() -> None:
    clock = Clock()
    breaker = make(clock)
    await trip(breaker)
    clock.now += 11
    with pytest.raises(LLMServerError):
        await breaker.call(boom)
    assert breaker.state is BreakerState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.call(ok)  # cool-off restarted
    clock.now += 11
    assert breaker.state is BreakerState.HALF_OPEN


async def test_only_one_probe_at_a_time_when_half_open() -> None:
    clock = Clock()
    breaker = make(clock)
    await trip(breaker)
    clock.now += 11
    gate = asyncio.Event()

    async def slow() -> str:
        await gate.wait()
        return "ok"

    probe = asyncio.create_task(breaker.call(slow))
    await asyncio.sleep(0)
    with pytest.raises(CircuitOpenError):
        await breaker.call(ok)  # second caller rejected while the probe runs
    gate.set()
    assert await probe == "ok"
    assert breaker.state is BreakerState.CLOSED


async def test_uncounted_errors_do_not_trip_the_breaker() -> None:
    async def bad_request() -> str:
        raise LLMBadRequestError("our fault")

    breaker = make(Clock(), should_count=lambda exc: isinstance(exc, LLMServerError))
    for _ in range(10):
        with pytest.raises(LLMBadRequestError):
            await breaker.call(bad_request)
    assert breaker.state is BreakerState.CLOSED


async def test_uncounted_error_during_probe_closes_circuit() -> None:
    clock = Clock()

    async def bad_request() -> str:
        raise LLMBadRequestError("our fault")

    breaker = make(clock, should_count=lambda exc: isinstance(exc, LLMServerError))
    await trip(breaker)
    clock.now += 11
    with pytest.raises(LLMBadRequestError):
        await breaker.call(bad_request)
    assert breaker.state is BreakerState.CLOSED
