"""Request pacing: never more than N requests in any 60-second window."""

import pytest

from app.core.llm.client import build_llm_client
from app.core.llm.rate_limit import RateLimiter
from tests.conftest import make_settings


class FakeTime:
    """A clock that only moves when the limiter sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_requests_within_the_limit_do_not_wait() -> None:
    t = FakeTime()
    limiter = RateLimiter(3, clock=t.clock, sleep=t.sleep)
    for _ in range(3):
        await limiter.acquire()
    assert t.sleeps == []


async def test_the_next_request_waits_until_the_oldest_leaves_the_window() -> None:
    t = FakeTime()
    limiter = RateLimiter(2, clock=t.clock, sleep=t.sleep)
    await limiter.acquire()
    t.now = 10.0
    await limiter.acquire()
    await limiter.acquire()  # third: must wait for the first (sent at 0) to age out at 60
    assert t.sleeps == [50.0] and t.now == 60.0


def test_a_limit_below_one_is_rejected() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0)


def test_gemini_is_paced_by_default_and_others_are_not() -> None:
    gem = make_settings(llm_provider="gemini", llm_model="g", gemini_api_key="k")
    assert gem.effective_llm_requests_per_minute == 8
    assert make_settings().effective_llm_requests_per_minute == 0
    groq = make_settings(llm_provider="groq", llm_model="m", groq_api_key="k")
    assert groq.effective_llm_requests_per_minute == 20
    assert (
        make_settings(
            llm_provider="gemini", llm_model="g", gemini_api_key="k", llm_requests_per_minute=0
        ).effective_llm_requests_per_minute
        == 0
    )


def test_blank_setting_from_compose_means_unset() -> None:
    assert make_settings(llm_requests_per_minute="").llm_requests_per_minute is None


def test_builder_attaches_a_limiter_only_when_paced() -> None:
    paced = build_llm_client(
        make_settings(llm_provider="gemini", llm_model="g", gemini_api_key="k")
    )
    assert paced._rate_limiter is not None  # type: ignore[attr-defined]
    assert build_llm_client(make_settings())._rate_limiter is None  # type: ignore[attr-defined]
