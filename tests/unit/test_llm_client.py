"""LLMClient: timeout, retry, breaker and usage recording working together."""

import asyncio
from decimal import Decimal

import pytest

from app.core.llm.base import (
    CircuitOpenError,
    LLMBadRequestError,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMServerError,
    LLMTimeoutError,
)
from app.core.llm.circuit_breaker import CircuitBreaker
from app.core.llm.client import CallContext, LLMClient, UsageRecord, build_llm_client
from app.core.llm.mock_provider import MockProvider
from app.core.llm.retry import RetryPolicy
from tests.conftest import make_settings

CTX = CallContext(agent="planner")
REQUEST = LLMRequest(system="sys", messages=[LLMMessage("user", "hello there")], task="chat")


class ListRecorder:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, usage: UsageRecord) -> None:
        self.records.append(usage)


def make_client(
    provider: MockProvider,
    recorder: ListRecorder,
    *,
    attempts: int = 3,
    threshold: int = 5,
    timeout: float = 1.0,
) -> LLMClient:
    return LLMClient(
        provider,
        retry_policy=RetryPolicy(max_attempts=attempts, base_delay=0.0, max_delay=0.0),
        breaker=CircuitBreaker(
            "llm",
            failure_threshold=threshold,
            recovery_seconds=60,
            should_count=lambda e: getattr(e, "retryable", False),
        ),
        timeout_seconds=timeout,
        recorder=recorder,
    )


async def test_mock_default_and_registered_handlers_are_deterministic() -> None:
    provider = MockProvider()
    provider.register("shout", lambda req: req.messages[-1].content.upper())
    client = make_client(provider, ListRecorder())
    assert (await client.complete(REQUEST, CTX)).text == "[mock:chat] hello there"
    shout = LLMRequest(system="s", messages=[LLMMessage("user", "hi")], task="shout")
    response = await client.complete(shout, CTX)
    assert response.text == "HI" and response.provider == "mock"
    assert response.prompt_tokens > 0 and response.completion_tokens > 0


async def test_success_is_recorded_with_usage() -> None:
    recorder = ListRecorder()
    await make_client(MockProvider(), recorder).complete(REQUEST, CallContext("review"))
    (record,) = recorder.records
    assert record.status == "SUCCEEDED" and record.context.agent == "review"
    assert record.prompt_tokens > 0 and record.model == "mock-model"


async def test_transient_errors_are_retried_and_every_attempt_recorded() -> None:
    provider, recorder = MockProvider(), ListRecorder()
    provider.fail_next(LLMServerError("503"), LLMServerError("503"))
    response = await make_client(provider, recorder).complete(REQUEST, CTX)
    assert response.text
    assert [r.status for r in recorder.records] == ["FAILED", "FAILED", "SUCCEEDED"]
    assert "LLMServerError" in (recorder.records[0].error or "")
    assert len(provider.calls) == 3


async def test_non_retryable_error_not_retried() -> None:
    provider, recorder = MockProvider(), ListRecorder()
    provider.fail_next(LLMBadRequestError("bad"))
    with pytest.raises(LLMBadRequestError):
        await make_client(provider, recorder).complete(REQUEST, CTX)
    assert len(provider.calls) == 1 and [r.status for r in recorder.records] == ["FAILED"]


async def test_timeout_becomes_retryable_timeout_error() -> None:
    provider, recorder = MockProvider(latency_seconds=0.5), ListRecorder()
    client = make_client(provider, recorder, attempts=2, timeout=0.05)
    with pytest.raises(LLMTimeoutError):
        await client.complete(REQUEST, CTX)
    assert [r.status for r in recorder.records] == ["TIMEOUT", "TIMEOUT"]


async def test_breaker_opens_then_fails_fast_without_calling_provider() -> None:
    provider, recorder = MockProvider(), ListRecorder()
    provider.fail_next(*[LLMServerError("down") for _ in range(10)])
    client = make_client(provider, recorder, attempts=1, threshold=2)
    for _ in range(2):
        with pytest.raises(LLMServerError):
            await client.complete(REQUEST, CTX)
    assert not client.is_healthy()
    calls_before = len(provider.calls)
    with pytest.raises(CircuitOpenError):
        await client.complete(REQUEST, CTX)
    assert len(provider.calls) == calls_before


async def test_retries_exhausted_counts_as_one_breaker_failure() -> None:
    provider, recorder = MockProvider(), ListRecorder()
    provider.fail_next(*[LLMServerError("down") for _ in range(3)])
    client = make_client(provider, recorder, attempts=3, threshold=2)
    with pytest.raises(LLMServerError):
        await client.complete(REQUEST, CTX)
    assert client.is_healthy()  # one logical failure < threshold of 2


async def test_cached_flag_and_latency_pass_through() -> None:
    class Cached(MockProvider):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                "x", "mock", "m", 1, 1, latency_ms=42, cost_usd=Decimal("0.5"), cached=True
            )

    recorder = ListRecorder()
    await make_client(Cached(), recorder).complete(REQUEST, CTX)
    assert recorder.records[0].cached and recorder.records[0].latency_ms == 42
    assert recorder.records[0].cost_usd == Decimal("0.5")


def test_builder_selects_provider_from_settings() -> None:
    mock_client = build_llm_client(make_settings())
    assert mock_client.provider.name == "mock" and mock_client.is_healthy()
    real = build_llm_client(
        make_settings(llm_provider="anthropic", llm_model="m-x", anthropic_api_key="k")
    )
    assert real.provider.name == "anthropic" and real.provider.model == "m-x"


async def test_cancellation_is_not_swallowed() -> None:
    client = make_client(MockProvider(latency_seconds=5), ListRecorder(), timeout=10)
    task = asyncio.create_task(client.complete(REQUEST, CTX))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
