"""ToolExecutor: registry + persistence + idempotent replay + audit.

What: runs a tool through the registry and records the attempt in ``tool_executions``
(inputs/outputs REDACTED). If an attempt with the same idempotency key already SUCCEEDED, the
stored result is returned and the tool is NOT run again.
Why: after a crash the orchestrator may re-dispatch the same step. Replaying from the stored
result (plus per-step dedupe inside side-effecting tools) means "at-least-once delivery"
never turns into "twice the side effects".
Audit: blocked calls and every MEDIUM/HIGH-risk execution are written to the audit log.
Connects to: ExecutionAgent (Phase 4), orchestrator (Phase 5).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit import AuditService
from app.core.observability.tracing import span
from app.core.redaction import redact
from app.metrics import TOOL_DURATION, TOOL_EXECUTIONS
from app.models import ToolExecution
from app.models.enums import CallStatus, RiskLevel
from app.tools.base import ToolContext, ToolError, ToolNotAllowedError, ToolTimeoutError
from app.tools.registry import ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditService,
    ) -> None:
        self._registry = registry
        self._session_factory = session_factory
        self._audit = audit

    async def execute(
        self,
        name: str,
        raw_inputs: dict[str, Any],
        ctx: ToolContext,
        *,
        attempt: int,
        plan_tools: Iterable[str],
        agent_allowlist: Iterable[str],
    ) -> ToolResult:
        if ctx.step_id is None:
            raise ValueError("ToolContext.step_id is required to execute a tool")
        # WHY the tool name is in the key: one step could in principle call several tools.
        key = ctx.idempotency_key or f"{ctx.step_id}:{name}:{attempt}"

        previous = await self._find_success(key)
        if previous is not None:
            logger.info("tool replayed from stored result", extra={"tool": name, "key": key})
            TOOL_EXECUTIONS.labels(name, "replayed").inc()
            return ToolResult(name, previous.output or {}, previous.duration_ms or 0, replayed=True)

        started = time.perf_counter()
        with span(f"tool.{name}", attempt=attempt):
            try:
                result = await self._registry.invoke(
                    name, raw_inputs, ctx, plan_tools=plan_tools, agent_allowlist=agent_allowlist
                )
            except ToolNotAllowedError as exc:
                TOOL_EXECUTIONS.labels(name, "blocked").inc()
                await self._audit_blocked(name, ctx, str(exc))
                raise
            except ToolError as exc:
                timed_out = isinstance(exc, ToolTimeoutError)
                status = CallStatus.TIMEOUT if timed_out else CallStatus.FAILED
                self._observe(name, status.value.lower(), started)
                await self._store(
                    name, raw_inputs, None, status, str(exc), started, attempt, key, ctx
                )
                raise
        self._observe(name, "succeeded", started)
        await self._store(
            name, raw_inputs, result.output, CallStatus.SUCCEEDED, None, started, attempt, key, ctx
        )
        await self._audit_if_risky(name, ctx, result)
        return result

    @staticmethod
    def _observe(name: str, status: str, started: float) -> None:
        TOOL_EXECUTIONS.labels(name, status).inc()
        TOOL_DURATION.labels(name).observe(time.perf_counter() - started)

    async def _find_success(self, key: str) -> ToolExecution | None:
        async with self._session_factory() as session:
            return await session.scalar(
                sa.select(ToolExecution).where(
                    ToolExecution.idempotency_key == key,
                    ToolExecution.status == CallStatus.SUCCEEDED,
                )
            )

    async def _store(
        self,
        name: str,
        raw_inputs: dict[str, Any],
        output: dict[str, Any] | None,
        status: CallStatus,
        error: str | None,
        started: float,
        attempt: int,
        key: str,
        ctx: ToolContext,
    ) -> None:
        assert ctx.step_id is not None
        try:
            async with self._session_factory() as session, session.begin():
                session.add(
                    ToolExecution(
                        step_id=ctx.step_id,
                        tool_name=name,
                        attempt=attempt,
                        idempotency_key=key,
                        input=redact(raw_inputs),  # WHY: traces are stored and shown in the UI
                        output=redact(output) if output is not None else None,
                        status=status,
                        error=error,
                        duration_ms=int((time.perf_counter() - started) * 1000),
                    )
                )
        except IntegrityError:
            logger.info("tool execution already recorded", extra={"key": key})

    async def _audit_blocked(self, name: str, ctx: ToolContext, reason: str) -> None:
        await self._audit.record(
            "tool.blocked", actor_id=ctx.principal.user_id, actor_email=ctx.principal.email,
            resource_type="tool", resource_id=name, outcome="denied",
            details={"reason": reason, "run_id": str(ctx.run_id), "step_id": str(ctx.step_id)},
        )  # fmt: skip

    async def _audit_if_risky(self, name: str, ctx: ToolContext, result: ToolResult) -> None:
        if self._registry.get(name).risk is RiskLevel.LOW:
            return
        await self._audit.record(
            "tool.executed", actor_id=ctx.principal.user_id, actor_email=ctx.principal.email,
            resource_type="tool", resource_id=name,
            details={
                "risk": self._registry.get(name).risk.value,
                "approved": ctx.approved,
                "run_id": str(ctx.run_id),
                "step_id": str(ctx.step_id),
                "duration_ms": result.duration_ms,
            },
        )  # fmt: skip
