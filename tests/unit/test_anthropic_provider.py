"""Anthropic adapter: response mapping and exception translation (no network)."""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from app.core.llm.anthropic_provider import AnthropicProvider, calculate_cost, translate_error
from app.core.llm.base import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMMessage,
    LLMRateLimitError,
    LLMRequest,
    LLMServerError,
    LLMTimeoutError,
)

REQ = LLMRequest(system="s", messages=[LLMMessage("user", "hi")])
HTTPX_REQ = httpx.Request("POST", "http://x")


def status_error(status: int, headers: dict[str, str] | None = None) -> anthropic.APIStatusError:
    response = httpx.Response(status, request=HTTPX_REQ, headers=headers or {})
    return anthropic.APIStatusError("boom", response=response, body=None)


class FakeMessages:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result, self.error, self.kwargs = result, error, {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.result


def provider(messages: FakeMessages, **kwargs: Any) -> AnthropicProvider:
    return AnthropicProvider(
        api_key="k",
        model="model-x",
        timeout_seconds=5,
        client=SimpleNamespace(messages=messages),
        **kwargs,
    )


async def test_maps_response_cost_and_model_from_config() -> None:
    text_block = anthropic.types.TextBlock(type="text", text="hello", citations=None)
    message = SimpleNamespace(
        content=[text_block],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
        stop_reason="end_turn",
    )
    fake = FakeMessages(message)
    result = await provider(fake, input_per_mtok=Decimal(3), output_per_mtok=Decimal(15)).complete(
        REQ
    )
    assert result.text == "hello" and result.model == "model-x" and result.provider == "anthropic"
    assert (result.prompt_tokens, result.completion_tokens) == (1000, 500)
    assert result.cost_usd == Decimal("0.0105")
    assert fake.kwargs["model"] == "model-x" and fake.kwargs["system"] == "s"
    assert fake.kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert "temperature" not in fake.kwargs


def test_cost_calculation() -> None:
    assert calculate_cost(0, 0, Decimal(3), Decimal(15)) == 0
    assert calculate_cost(2_000_000, 0, Decimal(3), Decimal(15)) == Decimal(6)


@pytest.mark.parametrize(
    ("error", "expected", "retryable"),
    [
        (status_error(429, {"retry-after": "7"}), LLMRateLimitError, True),
        (status_error(500), LLMServerError, True),
        (status_error(529), LLMServerError, True),
        (status_error(401), LLMAuthError, False),
        (status_error(403), LLMAuthError, False),
        (status_error(400), LLMBadRequestError, False),
        (anthropic.APITimeoutError(request=HTTPX_REQ), LLMTimeoutError, True),
        (anthropic.APIConnectionError(request=HTTPX_REQ), LLMConnectionError, True),
        (ValueError("weird"), LLMServerError, True),
    ],
)
def test_error_translation(error: Exception, expected: type, retryable: bool) -> None:
    translated = translate_error(error)
    assert isinstance(translated, expected) and translated.retryable is retryable


def test_retry_after_header_is_carried() -> None:
    assert translate_error(status_error(429, {"retry-after": "7"})).retry_after == 7.0
    assert translate_error(status_error(429, {"retry-after": "soon"})).retry_after is None
    assert translate_error(status_error(429)).retry_after is None


async def test_complete_raises_translated_error() -> None:
    with pytest.raises(LLMRateLimitError):
        await provider(FakeMessages(error=status_error(429))).complete(REQ)
