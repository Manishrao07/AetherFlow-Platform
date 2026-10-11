"""OpenAI-compatible (Groq) provider and the fallback wrapper, against a stubbed HTTP server."""

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

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
from app.core.llm.client import build_llm_client
from app.core.llm.fallback import FallbackProvider
from app.core.llm.mock_provider import MockProvider
from app.core.llm.openai_compat_provider import OpenAICompatProvider
from tests.conftest import make_settings

REQUEST = LLMRequest(system="be brief", messages=[LLMMessage("user", "hi")], max_tokens=50)


def provider(handler: httpx.MockTransport | None, model: str = "m-1") -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="groq",
        base_url="https://llm.test/openai/v1/",
        api_key="secret-key",
        model=model,
        timeout_seconds=5,
        input_per_mtok=Decimal(1),
        output_per_mtok=Decimal(2),
        transport=handler,
    )


def ok(text: str | None = "hello", model: str = "m-1") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        },
    )


async def test_request_shape_and_response_parsing() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return ok()

    result = await provider(httpx.MockTransport(handler)).complete(REQUEST)
    assert seen["url"] == "https://llm.test/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"
    body = seen["body"]
    assert isinstance(body, dict) and body["model"] == "m-1" and body["max_tokens"] == 50
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert result.text == "hello" and result.provider == "groq" and result.stop_reason == "stop"
    assert (result.prompt_tokens, result.completion_tokens) == (1000, 500)
    assert result.cost_usd == Decimal("0.002")  # 1000*1/1e6 + 500*2/1e6


async def test_null_content_becomes_empty_text() -> None:
    result = await provider(httpx.MockTransport(lambda r: ok(None))).complete(REQUEST)
    assert result.text == ""


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (429, {"retry-after": "7"}, LLMRateLimitError),
        (401, {}, LLMAuthError),
        (403, {}, LLMAuthError),
        (500, {}, LLMServerError),
        (503, {"retry-after": "oops"}, LLMServerError),
        (400, {}, LLMBadRequestError),
        (422, {}, LLMBadRequestError),
    ],
)
async def test_status_codes_map_to_the_error_taxonomy(
    status: int, headers: dict[str, str], expected: type[Exception]
) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, headers=headers))
    with pytest.raises(expected) as caught:
        await provider(transport).complete(REQUEST)
    if status == 429:
        assert caught.value.retry_after == 7  # type: ignore[attr-defined]


async def test_network_failures_are_retryable_errors() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(LLMTimeoutError):
        await provider(httpx.MockTransport(timeout)).complete(REQUEST)
    with pytest.raises(LLMConnectionError):
        await provider(httpx.MockTransport(refused)).complete(REQUEST)


@pytest.mark.parametrize("body", [{"nope": 1}, {"choices": []}, "not json at all"])
async def test_unreadable_responses_are_server_errors(body: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(200, content=body.encode())
        return httpx.Response(200, json=body)

    with pytest.raises(LLMServerError):
        await provider(httpx.MockTransport(handler)).complete(REQUEST)


async def test_fallback_is_used_for_transient_errors_only() -> None:
    primary = provider(httpx.MockTransport(lambda r: httpx.Response(429)), "big")
    backup = provider(httpx.MockTransport(lambda r: ok("from backup", "small")), "small")
    combined = FallbackProvider(primary, backup)
    assert combined.model == "big" and combined.name == "groq"
    result = await combined.complete(REQUEST)
    assert result.text == "from backup" and result.model == "small"

    broken_key = provider(httpx.MockTransport(lambda r: httpx.Response(401)), "big")
    with pytest.raises(LLMAuthError):  # a bad key would fail on the backup too: do not hide it
        await FallbackProvider(broken_key, backup).complete(REQUEST)


async def test_fallback_not_used_when_primary_works() -> None:
    boom = provider(httpx.MockTransport(lambda r: httpx.Response(500)), "small")
    combined = FallbackProvider(provider(httpx.MockTransport(lambda r: ok("primary")), "big"), boom)
    assert (await combined.complete(REQUEST)).text == "primary"
    assert FallbackProvider(MockProvider(), MockProvider()).model == "mock-model"


def test_groq_requires_model_and_key() -> None:
    with pytest.raises(ValidationError, match="LLM_MODEL"):
        make_settings(llm_provider="groq", groq_api_key="k")
    with pytest.raises(ValidationError, match="GROQ_API_KEY"):
        make_settings(llm_provider="groq", llm_model="m")


def test_builder_creates_groq_with_optional_fallback() -> None:
    plain = build_llm_client(make_settings(llm_provider="groq", llm_model="big", groq_api_key="k"))
    assert plain.provider.name == "groq" and plain.provider.model == "big"
    assert not isinstance(plain.provider, FallbackProvider)
    with_backup = build_llm_client(
        make_settings(
            llm_provider="groq", llm_model="big", llm_fallback_model="small", groq_api_key="k"
        )
    )
    assert isinstance(with_backup.provider, FallbackProvider)
    assert with_backup.provider.model == "big"


def test_gemini_requires_model_and_key() -> None:
    with pytest.raises(ValidationError, match="LLM_MODEL"):
        make_settings(llm_provider="gemini", gemini_api_key="k")
    with pytest.raises(ValidationError, match="GEMINI_API_KEY"):
        make_settings(llm_provider="gemini", llm_model="m")


def test_builder_creates_gemini_on_its_own_endpoint() -> None:
    client = build_llm_client(
        make_settings(llm_provider="gemini", llm_model="g-1", gemini_api_key="k")
    )
    assert client.provider.name == "gemini" and client.provider.model == "g-1"
    assert "generativelanguage.googleapis.com" in client.provider._url  # type: ignore[attr-defined]


async def test_bad_request_error_includes_the_providers_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model 'x' is not found"}})

    with pytest.raises(LLMBadRequestError, match=r"\(404\): model 'x' is not found"):
        await provider(httpx.MockTransport(handler)).complete(REQUEST)


async def test_bad_request_reason_handles_list_and_non_json_bodies() -> None:
    def listed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=[{"error": {"message": "bad\nthing"}}])

    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<html>nope</html>")

    with pytest.raises(LLMBadRequestError, match=r"\(400\): bad thing"):
        await provider(httpx.MockTransport(listed)).complete(REQUEST)
    with pytest.raises(LLMBadRequestError) as info:
        await provider(httpx.MockTransport(html)).complete(REQUEST)
    assert str(info.value).endswith("(404)")


async def test_headroom_is_added_to_the_output_limit_sent_to_the_provider() -> None:
    seen: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["max_tokens"] = json.loads(request.content)["max_tokens"]
        return ok()

    p = OpenAICompatProvider(
        name="gemini", base_url="https://llm.test/v1", api_key="k", model="m",
        timeout_seconds=5, output_headroom_tokens=1000, transport=httpx.MockTransport(handler),
    )  # fmt: skip
    await p.complete(REQUEST)
    assert seen["max_tokens"] == REQUEST.max_tokens + 1000


def test_gemini_gets_thinking_headroom_and_groq_does_not() -> None:
    gemini = build_llm_client(
        make_settings(llm_provider="gemini", llm_model="g", gemini_api_key="k")
    )
    groq = build_llm_client(make_settings(llm_provider="groq", llm_model="g", groq_api_key="k"))
    assert gemini.provider._headroom == 4096  # type: ignore[attr-defined]
    assert groq.provider._headroom == 0  # type: ignore[attr-defined]


async def test_reasoning_effort_is_sent_only_when_configured() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return ok()

    def make(effort: str | None) -> OpenAICompatProvider:
        return OpenAICompatProvider(
            name="gemini", base_url="https://llm.test/v1", api_key="k", model="m",
            timeout_seconds=5, reasoning_effort=effort, transport=httpx.MockTransport(handler),
        )  # fmt: skip

    await make("low").complete(REQUEST)
    await make(None).complete(REQUEST)
    assert bodies[0]["reasoning_effort"] == "low" and "reasoning_effort" not in bodies[1]


def test_gemini_defaults_to_low_effort_and_a_longer_timeout() -> None:
    gem = make_settings(llm_provider="gemini", llm_model="g", gemini_api_key="k")
    assert gem.gemini_reasoning_effort == "low"
    assert gem.effective_llm_timeout_seconds == 90.0
    explicit = make_settings(
        llm_provider="gemini", llm_model="g", gemini_api_key="k", llm_timeout_seconds=20
    )
    assert explicit.effective_llm_timeout_seconds == 20.0
    assert make_settings().effective_llm_timeout_seconds == 30.0


async def test_rate_limit_error_includes_the_providers_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "quota exceeded for this model"}})

    with pytest.raises(LLMRateLimitError, match="provider rate limit: quota exceeded"):
        await provider(httpx.MockTransport(handler)).complete(REQUEST)


def test_groq_thinking_models_get_low_effort_and_headroom_plain_models_do_not() -> None:
    def build(model: str) -> OpenAICompatProvider:
        client = build_llm_client(
            make_settings(llm_provider="groq", llm_model=model, groq_api_key="k")
        )
        return client.provider  # type: ignore[return-value]

    thinking = build("openai/gpt-oss-120b")
    assert thinking._headroom == 2048 and thinking._reasoning_effort == "low"
    plain = build("some/plain-chat-model")
    assert plain._headroom == 0 and plain._reasoning_effort is None
