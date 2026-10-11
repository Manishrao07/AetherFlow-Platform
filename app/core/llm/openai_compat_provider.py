"""OpenAI-compatible chat-completions provider (used for Groq).

What: a small ``httpx`` client for the ``/chat/completions`` endpoint that many vendors
(Groq, OpenRouter, vLLM, ...) expose, translated into our ``LLMProvider`` interface and our
retryable / non-retryable error taxonomy.
Why a hand-written client and not a vendor SDK: the wire format is tiny and stable, we already
own retries, backoff and the circuit breaker centrally (so SDK retries would hide failures from
our metrics), and it keeps one fewer dependency in the image.
Why the model name is a parameter: it comes from ``LLM_MODEL`` (never hardcoded); prices from
``LLM_*_COST_PER_MTOK``.
Connects to: built by ``client.build_llm_client`` when ``LLM_PROVIDER=groq``.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import httpx

from app.core.llm.anthropic_provider import calculate_cost
from app.core.llm.base import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMServerError,
    LLMTimeoutError,
)


def _provider_message(response: httpx.Response) -> str:
    """The provider's own explanation (e.g. "model not found"), short and single-line.

    WHY: "rejected request (404)" alone makes a wrong model name or URL a guessing game. Only
    the error text is kept (never headers or the request), and it is length-limited.
    """
    try:
        body = response.json()
        if isinstance(body, list) and body:  # Gemini sometimes wraps the error in a list
            body = body[0]
        error = body.get("error") if isinstance(body, dict) else None
        text = error.get("message") if isinstance(error, dict) else error
    except ValueError:
        text = None
    return " ".join(str(text or "").split())[:200]


def translate_status(response: httpx.Response) -> LLMError:
    """Map a non-2xx HTTP response to our taxonomy (keeps ``Retry-After`` when sent)."""
    status = response.status_code
    retry_after: float | None
    try:
        retry_after = float(response.headers["retry-after"])
    except (KeyError, ValueError):
        retry_after = None
    if status == 429:
        detail = _provider_message(response)
        return LLMRateLimitError(
            "provider rate limit" + (f": {detail}" if detail else ""), retry_after=retry_after
        )
    if status in (401, 403):
        return LLMAuthError("provider rejected credentials")
    if status >= 500:
        return LLMServerError(f"provider error {status}", retry_after=retry_after)
    detail = _provider_message(response)
    return LLMBadRequestError(
        f"provider rejected request ({status})" + (f": {detail}" if detail else "")
    )


class OpenAICompatProvider(LLMProvider):
    """Calls ``POST {base_url}/chat/completions`` with a bearer token."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | list[str],
        model: str,
        timeout_seconds: float,
        input_per_mtok: Decimal = Decimal(0),
        output_per_mtok: Decimal = Decimal(0),
        output_headroom_tokens: int = 0,
        reasoning_effort: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        # WHY: "thinking" models (Gemini) count their hidden reasoning against max_tokens, so a
        # small limit can be spent before a single visible word is written. The headroom is added
        # on top of what the caller asked for; the caller's own limit still applies to the text.
        self._headroom = output_headroom_tokens
        # WHY: thinking models can spend a long time reasoning before answering; "low" keeps
        # answers fast and cheap, which matters for planning/review calls that need structure,
        # not deep thought. None = leave the provider's default.
        self._reasoning_effort = reasoning_effort
        self.model = model
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_keys = [api_key] if isinstance(api_key, str) else api_key
        self._input_price = input_per_mtok
        self._output_price = output_per_mtok
        self._timeout = timeout_seconds
        self._transport = transport  # injectable for tests; None = real network

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        import secrets

        api_key = secrets.choice(self._api_keys)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens + self._headroom,
            "messages": [
                {"role": "system", "content": request.system},
                *({"role": m.role, "content": m.content} for m in request.messages),
            ],
        }
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
                response = await http.post(self._url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("provider request timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMConnectionError("could not reach provider") from exc
        if response.status_code >= 400:
            raise translate_status(response)
        return self._parse(response, started)

    def _parse(self, response: httpx.Response, started: float) -> LLMResponse:
        try:
            body = response.json()
            choice = body["choices"][0]
            usage = body.get("usage") or {}
            # WHY "or ''": some models return null content when they only produced reasoning.
            text = choice["message"].get("content") or ""
            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMServerError("provider returned an unreadable response") from exc
        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(body.get("model") or self.model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=calculate_cost(
                prompt_tokens, completion_tokens, self._input_price, self._output_price
            ),
            stop_reason=choice.get("finish_reason"),
        )
