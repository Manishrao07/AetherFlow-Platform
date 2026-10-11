"""Anthropic (Claude) provider.

What: adapts the official ``anthropic`` SDK to our ``LLMProvider`` interface and translates
its exceptions into our retryable / non-retryable taxonomy.
Why: the rest of the system never sees SDK types. The model name and prices come from
configuration (``LLM_MODEL``, ``LLM_*_COST_PER_MTOK``) - nothing vendor-specific is hardcoded.
Why ``max_retries=0`` on the SDK: we own retry/backoff/breaker logic centrally so behaviour
and metrics are identical for every provider (the SDK would retry silently underneath us).
Connects to: built by ``client.build_llm_client`` when ``LLM_PROVIDER=anthropic``.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import anthropic

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


def calculate_cost(
    prompt_tokens: int, completion_tokens: int, input_per_mtok: Decimal, output_per_mtok: Decimal
) -> Decimal:
    """USD cost estimate from token counts and per-million-token prices."""
    return (
        Decimal(prompt_tokens) * input_per_mtok + Decimal(completion_tokens) * output_per_mtok
    ) / Decimal(1_000_000)


def translate_error(exc: Exception) -> LLMError:
    """Map an SDK exception to our taxonomy (keeps Retry-After when the server sent one)."""
    if isinstance(exc, anthropic.APITimeoutError):
        return LLMTimeoutError("provider request timed out")
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMConnectionError("could not reach provider")
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        retry_after = _retry_after(exc)
        if status == 429:
            return LLMRateLimitError("provider rate limit", retry_after=retry_after)
        if status in (401, 403):
            return LLMAuthError("provider rejected credentials")
        if status >= 500 or status == 529:  # 529 = Anthropic "overloaded"
            return LLMServerError(f"provider error {status}", retry_after=retry_after)
        return LLMBadRequestError(f"provider rejected request ({status})")
    return LLMServerError(f"unexpected provider failure: {type(exc).__name__}")


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        input_per_mtok: Decimal = Decimal(0),
        output_per_mtok: Decimal = Decimal(0),
        client: Any | None = None,
    ) -> None:
        self.model = model
        self._input_price = input_per_mtok
        self._output_price = output_per_mtok
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=0
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        try:
            message = await self._client.messages.create(
                model=self.model,
                system=request.system,
                messages=[{"role": m.role, "content": m.content} for m in request.messages],
                max_tokens=request.max_tokens,
            )
        except Exception as exc:
            raise translate_error(exc) from exc
        # WHY isinstance: the content union has many block types; we only want plain text.
        text = "".join(b.text for b in message.content if isinstance(b, anthropic.types.TextBlock))
        usage = message.usage
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=calculate_cost(
                usage.input_tokens, usage.output_tokens, self._input_price, self._output_price
            ),
            stop_reason=message.stop_reason,
        )
