"""Deterministic fake LLM so the whole system runs and tests pass with no API key.

What: ``MockProvider`` answers requests from registered per-task handlers (plain Python
functions), estimates token counts, and can inject failures for resilience tests.
Why: demos, CI and tests must be repeatable and free. The Planner/Review agents (Phase 4)
register handlers that produce valid, schema-conforming JSON for their own task labels.
Connects to: ``client.py`` (wrapped like any provider); agents register handlers on startup.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from decimal import Decimal

from app.core.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse

Handler = Callable[[LLMRequest], str]


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _default_handler(request: LLMRequest) -> str:
    last = request.messages[-1].content if request.messages else ""
    return f"[mock:{request.task}] {last[:200]}"


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(self, model: str = "mock-model", latency_seconds: float = 0.0) -> None:
        self.model = model
        self._latency = latency_seconds
        self._handlers: dict[str, Handler] = {}
        self._failures: list[LLMError] = []
        self.calls: list[LLMRequest] = []

    def register(self, task: str, handler: Handler) -> None:
        """Teach the mock how to answer requests labelled ``task``."""
        self._handlers[task] = handler

    def fail_next(self, *errors: LLMError) -> None:
        """Make the next ``len(errors)`` calls raise these errors (in order)."""
        self._failures.extend(errors)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self._latency:
            await asyncio.sleep(self._latency)
        if self._failures:
            raise self._failures.pop(0)
        text = self._handlers.get(request.task, _default_handler)(request)
        prompt = request.system + "".join(m.content for m in request.messages)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=_estimate_tokens(prompt),
            completion_tokens=_estimate_tokens(text),
            cost_usd=Decimal(0),
            stop_reason="end_turn",
        )
