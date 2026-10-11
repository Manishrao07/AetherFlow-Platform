"""Persist every LLM attempt to ``llm_calls`` (tokens, latency, cost, outcome).

What: ``DbUsageRecorder`` implements the generic ``UsageRecorder`` protocol.
Why own transaction: usage must be recorded even when the caller's transaction later rolls
back (a failed attempt still happened and may have cost money).
Why swallow errors: cost accounting must never break a user request.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.llm.client import UsageRecord
from app.models import LLMCall
from app.models.enums import CallStatus, LLMCaller

logger = logging.getLogger(__name__)


class DbUsageRecorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, usage: UsageRecord) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                session.add(
                    LLMCall(
                        run_id=usage.context.run_id,
                        step_id=usage.context.step_id,
                        agent=LLMCaller(usage.context.agent),
                        provider=usage.provider,
                        model=usage.model,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        latency_ms=usage.latency_ms,
                        cost_usd=usage.cost_usd,
                        cached=usage.cached,
                        status=CallStatus(usage.status),
                        error=(usage.error or None) and usage.error[:2000],
                    )
                )
        except Exception:
            logger.exception("failed to record llm usage")
