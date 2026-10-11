"""Backoff maths and the retry loop."""

import random

import pytest

from app.core.llm.base import LLMBadRequestError, LLMRateLimitError, LLMServerError
from app.core.llm.retry import RetryPolicy, backoff_delay, retry_async

POLICY = RetryPolicy(max_attempts=4, base_delay=1.0, max_delay=5.0)


def retryable(exc: BaseException) -> bool:
    return bool(getattr(exc, "retryable", False))


def test_backoff_is_jittered_within_exponential_cap() -> None:
    rng = random.Random(7)
    for attempt, cap in [(1, 1.0), (2, 2.0), (3, 4.0), (4, 5.0), (9, 5.0)]:
        samples = [backoff_delay(attempt, POLICY, rng) for _ in range(200)]
        assert all(0 <= s <= cap for s in samples)
        assert max(samples) > cap * 0.5  # genuinely spread out, not a constant


def test_retry_after_overrides_backoff_but_is_capped() -> None:
    assert backoff_delay(1, POLICY, retry_after=3.0) == 3.0
    assert backoff_delay(1, POLICY, retry_after=999) == POLICY.max_delay


def test_invalid_policy_rejected() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=5, max_delay=1)


async def test_succeeds_after_transient_failures() -> None:
    sleeps: list[float] = []
    calls: list[int] = []

    async def flaky(attempt: int) -> str:
        calls.append(attempt)
        if attempt < 3:
            raise LLMServerError("503")
        return "ok"

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    assert await retry_async(flaky, POLICY, is_retryable=retryable, sleep=fake_sleep) == "ok"
    assert calls == [1, 2, 3] and len(sleeps) == 2


async def test_non_retryable_error_is_raised_immediately() -> None:
    calls = 0

    async def bad(attempt: int) -> None:
        nonlocal calls
        calls += 1
        raise LLMBadRequestError("nope")

    with pytest.raises(LLMBadRequestError):
        await retry_async(bad, POLICY, is_retryable=retryable)
    assert calls == 1


async def test_gives_up_after_max_attempts_and_keeps_original_error() -> None:
    calls = 0

    async def always(attempt: int) -> None:
        nonlocal calls
        calls += 1
        raise LLMRateLimitError("429", retry_after=0.0)

    async def no_sleep(_: float) -> None:
        return None

    with pytest.raises(LLMRateLimitError):
        await retry_async(always, POLICY, is_retryable=retryable, sleep=no_sleep)
    assert calls == POLICY.max_attempts


async def test_on_retry_callback_and_retry_after_used() -> None:
    seen: list[tuple[int, float]] = []
    slept: list[float] = []

    async def fn(attempt: int) -> int:
        if attempt == 1:
            raise LLMRateLimitError("429", retry_after=2.0)
        return 1

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    await retry_async(
        fn,
        POLICY,
        is_retryable=retryable,
        sleep=fake_sleep,
        on_retry=lambda attempt, exc, delay: seen.append((attempt, delay)),
    )
    assert seen == [(1, 2.0)] and slept == [2.0]
