"""Fallback provider: try the primary model, then a backup model.

What: wraps two providers behind the ``LLMProvider`` interface. If the primary fails with a
transient error (timeout, rate limit, 5xx, connection) the same request goes to the backup.
Why: a rate-limited or briefly unavailable big model should not fail a whole workflow when a
smaller model can still answer. This is the "retry on another model" idea from the research
agent, kept generic here so any provider pair works.
Why only transient errors: a bad request or bad API key will fail on the backup too (same
vendor, same credentials), so we surface it immediately instead of hiding a configuration bug.
Connects to: built by ``client.build_llm_client`` when ``LLM_FALLBACK_MODEL`` is set.
"""

from __future__ import annotations

import logging

from app.core.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)


class FallbackProvider(LLMProvider):
    def __init__(self, primary: LLMProvider, backup: LLMProvider) -> None:
        self.name = primary.name
        self.model = primary.model  # WHY: the *configured* model; responses report the real one
        self._primary = primary
        self._backup = backup

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            return await self._primary.complete(request)
        except LLMError as exc:
            if not exc.retryable:
                raise
            logger.warning(
                "primary model failed, using fallback",
                extra={"primary": self._primary.model, "fallback": self._backup.model,
                       "error": type(exc).__name__},
            )  # fmt: skip
            return await self._backup.complete(request)
