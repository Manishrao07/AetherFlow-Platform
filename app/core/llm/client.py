"""LLMClient: the one object agents call.

What: wraps any provider with a hard timeout, retry with backoff, a circuit breaker and a
usage recorder that sees EVERY attempt (success or failure).
Order of protection (outside -> inside): cache -> breaker -> retry loop -> timeout -> provider.
The breaker sits outside the retry loop so one logical call that exhausts its retries counts
as one failure, and an open circuit skips the retries entirely (fail fast).
Cache (optional): identical requests are answered from the cache without touching the provider
or the breaker, so repeated work costs nothing and still works during a provider outage. The
CALLER decides when an answer was bad (``evict``), so a malformed answer is never served twice.
Why a recorder callback: this module is generic (``core/``); persisting rows to Project 1's
``llm_calls`` table is done by an injected recorder, so Project 2 can reuse the client.
Connects to: agents (Phase 4), ``services/llm_usage.py`` (recorder), ``/readyz``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.core.cache import Cache, MeteredCache
from app.core.config import LLMProviderName, Settings
from app.core.llm.anthropic_provider import AnthropicProvider
from app.core.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse, LLMTimeoutError
from app.core.llm.circuit_breaker import CircuitBreaker
from app.core.llm.fallback import FallbackProvider
from app.core.llm.mock_provider import MockProvider
from app.core.llm.openai_compat_provider import OpenAICompatProvider
from app.core.llm.rate_limit import RateLimiter
from app.core.llm.retry import RetryPolicy, retry_async
from app.core.observability.metrics import BREAKERS, observe_llm
from app.core.observability.tracing import span

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallContext:
    """Who is asking - used only for usage records and logs, never sent to the provider."""

    agent: str
    run_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None


@dataclass(frozen=True)
class UsageRecord:
    context: CallContext
    provider: str
    model: str
    status: str  # SUCCEEDED | FAILED | TIMEOUT
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_usd: Decimal = Decimal(0)
    cached: bool = False
    error: str | None = None


class UsageRecorder(Protocol):
    async def record(self, usage: UsageRecord) -> None: ...


class NullUsageRecorder:
    async def record(self, usage: UsageRecord) -> None:
        return None


class LLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        retry_policy: RetryPolicy,
        breaker: CircuitBreaker,
        timeout_seconds: float,
        recorder: UsageRecorder | None = None,
        cache: Cache | None = None,
        cache_ttl_seconds: int = 3600,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.provider = provider
        self._rate_limiter = rate_limiter
        self._retry = retry_policy
        self._breaker = breaker
        self._timeout = timeout_seconds
        self._recorder = recorder or NullUsageRecorder()
        self._cache = MeteredCache(cache, "llm") if cache is not None else None
        self._cache_ttl = cache_ttl_seconds
        BREAKERS.register(breaker.name, lambda: breaker.state.value)

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def is_healthy(self) -> bool:
        """Cheap readiness signal: provider is configured and the breaker is not open."""
        return not self._breaker.is_open

    async def complete(self, request: LLMRequest, context: CallContext) -> LLMResponse:
        """Run a completion with all protections. Raises ``LLMError`` subclasses."""
        with span(
            f"llm.{request.task}",
            agent=context.agent,
            provider=self.provider.name,
            model=self.provider.model,
        ) as active:
            hit = await self._cache_lookup(request)
            if hit is not None:
                await self._record_cache_hit(context, hit)
                active.set(cached=True)
                return hit
            response = await self._breaker.call(lambda: self._with_retry(request, context))
            active.set(
                cached=False,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
            await self._cache_store(request, response)
            return response

    async def evict(self, request: LLMRequest) -> None:
        """Forget a cached answer the caller found unusable (e.g. invalid JSON)."""
        if self._cache is not None:
            await self._cache.delete(self._cache_key(request))

    def _cache_key(self, request: LLMRequest) -> str:
        # WHY every field: any change to the prompt, model or limits must be a different entry.
        material = json.dumps(
            [
                self.provider.name,
                self.provider.model,
                request.task,
                request.system,
                [(m.role, m.content) for m in request.messages],
                request.max_tokens,
            ],
            ensure_ascii=False,
        )
        return "llm:" + hashlib.sha256(material.encode()).hexdigest()

    async def _cache_lookup(self, request: LLMRequest) -> LLMResponse | None:
        if self._cache is None:
            return None
        raw = await self._cache.get(self._cache_key(request))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return LLMResponse(
                text=data["text"], provider=data["provider"], model=data["model"],
                prompt_tokens=data["prompt_tokens"], completion_tokens=data["completion_tokens"],
                stop_reason=data.get("stop_reason"), cached=True,
            )  # fmt: skip
        except (ValueError, KeyError, TypeError):
            return None  # a corrupt entry is just a miss

    async def _cache_store(self, request: LLMRequest, response: LLMResponse) -> None:
        if self._cache is None:
            return
        payload = {
            "text": response.text, "provider": response.provider, "model": response.model,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens, "stop_reason": response.stop_reason,
        }  # fmt: skip
        await self._cache.set(self._cache_key(request), json.dumps(payload), self._cache_ttl)

    async def _record_cache_hit(self, context: CallContext, response: LLMResponse) -> None:
        # WHY zero tokens/cost: nothing was spent, and totals should show what was actually paid.
        await self._recorder.record(
            UsageRecord(
                context=context, provider=response.provider, model=response.model,
                status="SUCCEEDED", cached=True,
            )
        )  # fmt: skip

    async def _with_retry(self, request: LLMRequest, context: CallContext) -> LLMResponse:
        return await retry_async(
            lambda attempt: self._attempt(request, context),
            self._retry,
            is_retryable=lambda exc: isinstance(exc, LLMError) and exc.retryable,
        )

    async def _attempt(self, request: LLMRequest, context: CallContext) -> LLMResponse:
        if self._rate_limiter is not None:
            await self._rate_limiter.acquire()  # waits for a free slot; not part of the timeout
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                response = await self.provider.complete(request)
        except TimeoutError as exc:
            error: LLMError = LLMTimeoutError(f"no response within {self._timeout}s")
            await self._record_failure(context, started, error, "TIMEOUT")
            raise error from exc
        except LLMError as exc:
            await self._record_failure(context, started, exc, "FAILED")
            raise
        latency_ms = int((time.perf_counter() - started) * 1000)
        observe_llm(
            provider=response.provider, model=response.model, agent=context.agent,
            status="succeeded", seconds=time.perf_counter() - started,
            prompt_tokens=response.prompt_tokens, completion_tokens=response.completion_tokens,
            cost_usd=float(response.cost_usd),
        )  # fmt: skip
        await self._recorder.record(
            UsageRecord(
                context=context,
                provider=response.provider,
                model=response.model,
                status="SUCCEEDED",
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                latency_ms=response.latency_ms or latency_ms,
                cost_usd=response.cost_usd,
                cached=response.cached,
            )
        )
        logger.info(
            "llm call",
            extra={
                "agent": context.agent,
                "task": request.task,
                "model": response.model,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "latency_ms": latency_ms,
            },
        )
        return response

    async def _record_failure(
        self, context: CallContext, started: float, error: LLMError, status: str
    ) -> None:
        observe_llm(
            provider=self.provider.name, model=self.provider.model, agent=context.agent,
            status=status.lower(), seconds=time.perf_counter() - started,
        )  # fmt: skip
        await self._recorder.record(
            UsageRecord(
                context=context,
                provider=self.provider.name,
                model=self.provider.model,
                status=status,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(error).__name__}: {error}",
            )
        )


def build_llm_client(
    settings: Settings, recorder: UsageRecorder | None = None, cache: Cache | None = None
) -> LLMClient:
    """Create the provider selected by ``LLM_PROVIDER`` and wrap it with the protections."""
    provider: LLMProvider
    if settings.llm_provider is LLMProviderName.ANTHROPIC:
        assert settings.anthropic_api_key is not None and settings.llm_model  # validated at boot
        provider = AnthropicProvider(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            input_per_mtok=settings.llm_input_cost_per_mtok,
            output_per_mtok=settings.llm_output_cost_per_mtok,
        )
    elif settings.llm_provider in (LLMProviderName.GROQ, LLMProviderName.GEMINI):
        # WHY one branch: Groq and Gemini both speak the OpenAI chat-completions protocol, so
        # only the name, URL and key differ.
        assert settings.llm_model  # validated at boot
        is_groq = settings.llm_provider is LLMProviderName.GROQ
        key = settings.groq_api_key if is_groq else settings.gemini_api_key
        base_url = settings.groq_base_url if is_groq else settings.gemini_base_url
        assert key is not None  # validated at boot

        def compat(model: str) -> OpenAICompatProvider:
            # WHY per model: "thinking" models (Gemini, Groq's gpt-oss) spend hidden tokens on
            # reasoning, so they get a lower reasoning effort and extra output room; plain chat
            # models are sent exactly what the caller asked for.
            if is_groq:
                thinks = "gpt-oss" in model
                headroom, effort = (2048, "low") if thinks else (0, None)
            else:
                headroom = settings.gemini_thinking_headroom_tokens
                effort = settings.gemini_reasoning_effort or None

            raw_key = key.get_secret_value()
            keys = [k.strip() for k in raw_key.split(",")] if "," in raw_key else raw_key

            return OpenAICompatProvider(
                name=settings.llm_provider.value,
                base_url=base_url,
                api_key=keys,
                model=model,
                timeout_seconds=settings.effective_llm_timeout_seconds,
                input_per_mtok=settings.llm_input_cost_per_mtok,
                output_per_mtok=settings.llm_output_cost_per_mtok,
                output_headroom_tokens=headroom,
                reasoning_effort=effort,
            )

        provider = compat(settings.llm_model)
        if settings.llm_fallback_model:
            provider = FallbackProvider(provider, compat(settings.llm_fallback_model))
    else:
        provider = MockProvider(
            model=settings.effective_llm_model, latency_seconds=settings.mock_llm_latency_seconds
        )
    breaker = CircuitBreaker(
        f"llm:{provider.name}",
        failure_threshold=settings.llm_breaker_failure_threshold,
        recovery_seconds=settings.llm_breaker_recovery_seconds,
        # WHY: a rejected request (400) or bad key is our configuration problem; it must not
        # trip the breaker as if the provider were unhealthy.
        should_count=lambda exc: isinstance(exc, LLMError) and exc.retryable,
    )
    return LLMClient(
        provider,
        retry_policy=RetryPolicy(
            max_attempts=settings.llm_max_attempts,
            base_delay=settings.llm_retry_base_delay_seconds,
            # WHY 30: providers say "retry in 12s" on a rate limit; obeying that beats failing.
            max_delay=30.0,
        ),
        breaker=breaker,
        timeout_seconds=settings.effective_llm_timeout_seconds,
        recorder=recorder,
        cache=cache if settings.llm_cache_enabled else None,
        cache_ttl_seconds=settings.llm_cache_ttl_seconds,
        rate_limiter=RateLimiter(rpm)
        if (rpm := settings.effective_llm_requests_per_minute)
        else None,
    )
