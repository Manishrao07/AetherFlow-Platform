"""Workflow use-cases for the API: create, read, list, cancel, retry a step, timeline.

What: ``WorkflowService`` - everything a client may do to a run EXCEPT executing it (that is the
engine's job). It validates ownership/visibility, performs the state change through ``RunStore``
and wakes a worker.
Visibility: a run is visible to its owner, to admins and to approvers (they must be able to see
what they are asked to approve). Anyone else gets 404, not 403, so ids cannot be probed.
Why commit BEFORE enqueue: the worker runs in another transaction; it must be able to see the
row. (Enqueue-then-commit is a classic race that makes the worker report "run not found".)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.plan import Plan
from app.api.schemas.common import PageParams
from app.core.audit import AuditService
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.rbac import Principal
from app.metrics import WORKFLOWS_CREATED
from app.models import (
    Approval,
    LLMCall,
    RunEvent,
    ToolExecution,
    WorkflowRun,
    WorkflowStep,
)
from app.models.enums import ApprovalStatus, RoleName, RunMode, RunStatus, StepStatus
from app.orchestrator.approvals import ApprovalView, EnqueueFn, approval_views_for_run
from app.orchestrator.state_machine import TERMINAL_RUN_STATUSES
from app.orchestrator.store import RunStore, add_event
from app.services.conversations import ConversationService

_SEE_ALL = {RoleName.ADMIN.value, RoleName.APPROVER.value}


@dataclass(frozen=True)
class Timeline:
    run: WorkflowRun
    steps: list[WorkflowStep]
    tool_executions: list[ToolExecution]
    llm_calls: list[LLMCall]
    approvals: list[ApprovalView]
    events: list[RunEvent]


class WorkflowService:
    def __init__(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        store: RunStore,
        audit: AuditService,
        enqueue: EnqueueFn,
        default_max_revisions: int,
        step_max_attempts: int,
    ) -> None:
        self._session = session
        self._sf = session_factory
        self._store = store
        self._audit = audit
        self._enqueue = enqueue
        self._default_max_revisions = default_max_revisions
        self._step_max_attempts = step_max_attempts

    # ------------------------------------------------------------------ create
    async def create(
        self,
        principal: Principal,
        goal: str,
        *,
        conversation_id: uuid.UUID | None,
        max_revisions: int | None,
        idempotency_key: str | None,
        ip: str | None,
        mode: RunMode = RunMode.STANDARD,
    ) -> tuple[WorkflowRun, bool]:
        """Start a run -> ``(run, created)``; a repeated Idempotency-Key returns the original."""
        if conversation_id is not None:  # ownership + archived checks, before anything is written
            await ConversationService(self._session).get(principal.user_id, conversation_id)
        inserted = await self._session.scalar(
            insert(WorkflowRun)
            .values(
                user_id=principal.user_id,
                conversation_id=conversation_id,
                goal=goal.strip(),
                mode=mode,
                max_revisions=(
                    self._default_max_revisions if max_revisions is None else max_revisions
                ),
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(constraint="uq_workflow_runs_user_idempotency_key")
            .returning(WorkflowRun.id)
        )
        if inserted is None:  # same key as an earlier request: hand back that run
            existing = await self._session.scalar(
                sa.select(WorkflowRun).where(
                    WorkflowRun.user_id == principal.user_id,
                    WorkflowRun.idempotency_key == idempotency_key,
                )
            )
            assert existing is not None
            return existing, False
        run = await self._session.get(WorkflowRun, inserted)
        assert run is not None
        add_event(self._session, run.id, "run.created", {"goal": run.goal[:200]})
        if conversation_id is not None:
            await ConversationService(self._session).append(
                principal.user_id, conversation_id, run.goal, run_id=run.id
            )
        await self._session.commit()
        WORKFLOWS_CREATED.inc()
        await self._audit.record(
            "workflow.created", actor_id=principal.user_id, actor_email=principal.email,
            resource_type="workflow_run", resource_id=run.id, ip_address=ip,
            details={"goal": run.goal[:200], "conversation_id": str(conversation_id)},
        )  # fmt: skip
        await self._enqueue(run.id)
        return run, True

    # -------------------------------------------------------------------- read
    def _can_see(self, principal: Principal, run: WorkflowRun) -> bool:
        return run.user_id == principal.user_id or principal.has_any_role(_SEE_ALL)

    async def get(self, principal: Principal, run_id: uuid.UUID) -> WorkflowRun:
        run = await self._session.get(WorkflowRun, run_id, populate_existing=True)
        if run is None or not self._can_see(principal, run):
            raise NotFoundError("Workflow run not found")
        return run

    async def list_runs(
        self,
        principal: Principal,
        page: PageParams,
        status: RunStatus | None,
        everyone: bool,
    ) -> tuple[list[WorkflowRun], int]:
        conditions = []
        if not (everyone and principal.has_any_role(_SEE_ALL)):
            conditions.append(WorkflowRun.user_id == principal.user_id)
        if status is not None:
            conditions.append(WorkflowRun.status == status)
        total = (
            await self._session.scalar(
                sa.select(sa.func.count()).select_from(WorkflowRun).where(*conditions)
            )
            or 0
        )
        rows = await self._session.scalars(
            sa.select(WorkflowRun)
            .where(*conditions)
            .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id)
            .limit(page.limit)
            .offset(page.offset)
        )
        return list(rows), total

    async def timeline(self, principal: Principal, run_id: uuid.UUID) -> Timeline:
        run = await self.get(principal, run_id)
        steps = list(
            await self._session.scalars(
                sa.select(WorkflowStep)
                .where(WorkflowStep.run_id == run_id)
                .order_by(WorkflowStep.revision, WorkflowStep.position)
            )
        )
        step_ids = [s.id for s in steps]
        tools = (
            list(
                await self._session.scalars(
                    sa.select(ToolExecution)
                    .where(ToolExecution.step_id.in_(step_ids))
                    .order_by(ToolExecution.created_at, ToolExecution.attempt)
                )
            )
            if step_ids
            else []
        )
        llm_calls = list(
            await self._session.scalars(
                sa.select(LLMCall).where(LLMCall.run_id == run_id).order_by(LLMCall.created_at)
            )
        )
        approvals = await approval_views_for_run(self._session, run_id)
        events = await self.events_after(run_id, 0, limit=1000)
        return Timeline(run, steps, tools, llm_calls, approvals, events)

    async def events_after(
        self, run_id: uuid.UUID, after_id: int, limit: int = 200
    ) -> list[RunEvent]:
        rows = await self._session.scalars(
            sa.select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.id > after_id)
            .order_by(RunEvent.id)
            .limit(limit)
        )
        return list(rows)

    # ------------------------------------------------------------------ cancel
    async def cancel(self, principal: Principal, run_id: uuid.UUID, ip: str | None) -> WorkflowRun:
        run = await self.get(principal, run_id)
        self._require_owner_or_admin(principal, run)
        if run.status in TERMINAL_RUN_STATUSES:
            raise ConflictError(f"Run is already {run.status.value}")

        async def close_pending_approvals(session: AsyncSession, current: WorkflowRun) -> None:
            await session.execute(
                sa.update(Approval)
                .where(Approval.run_id == current.id, Approval.status == ApprovalStatus.PENDING)
                .values(status=ApprovalStatus.EXPIRED, comment="run cancelled")
            )

        previous_status = run.status.value
        await self._store.transition_run(
            run_id, RunStatus.CANCELLED, reason=f"cancelled by {principal.email}",
            error=f"Cancelled by {principal.email}", extra=close_pending_approvals,
        )  # fmt: skip
        await self._audit.record(
            "workflow.cancelled", actor_id=principal.user_id, actor_email=principal.email,
            resource_type="workflow_run", resource_id=run_id, ip_address=ip,
            details={"previous_status": previous_status},
        )  # fmt: skip
        # WHY re-read: the store used its own session; return a copy bound to this request's.
        return await self.get(principal, run_id)

    # -------------------------------------------------------------- retry-step
    async def retry_step(
        self, principal: Principal, run_id: uuid.UUID, step_id: uuid.UUID, ip: str | None
    ) -> WorkflowRun:
        """Re-run one FAILED step (and the steps skipped because of it) of a FAILED run."""
        run = await self.get(principal, run_id)
        self._require_owner_or_admin(principal, run)
        step = await self._session.get(WorkflowStep, step_id)
        if step is None or step.run_id != run_id:
            raise NotFoundError("Workflow step not found")
        if run.status is not RunStatus.FAILED:
            raise ConflictError("Only steps of a FAILED run can be retried")
        if step.status is not StepStatus.FAILED or step.revision != run.revision_count:
            raise ConflictError("Only a FAILED step of the current plan can be retried")

        plan = Plan.model_validate(run.plan)
        blocked = _dependents(plan, step.step_key)
        siblings = list(
            await self._session.scalars(
                sa.select(WorkflowStep).where(
                    WorkflowStep.run_id == run_id, WorkflowStep.revision == run.revision_count
                )
            )
        )
        # WHY max_attempts = attempts + N: keeps attempt numbers (and idempotency keys) unique
        # across the retry, so earlier failed attempts stay visible and nothing collides.
        await self._store.transition_step(
            step_id, StepStatus.PENDING, reason=f"retried by {principal.email}", error=None,
            max_attempts=step.attempts + self._step_max_attempts,
        )  # fmt: skip
        for other in siblings:
            if other.step_key in blocked and other.status is StepStatus.SKIPPED:
                await self._store.transition_step(
                    other.id, StepStatus.PENDING, reason="upstream step retried"
                )
        await self._store.transition_run(
            run_id, RunStatus.EXECUTING, reason=f"step '{step.step_key}' retried", error=None
        )
        await self._audit.record(
            "workflow.step_retried", actor_id=principal.user_id, actor_email=principal.email,
            resource_type="workflow_step", resource_id=step_id, ip_address=ip,
            details={"run_id": str(run_id), "step_key": step.step_key},
        )  # fmt: skip
        await self._enqueue(run_id)
        return await self.get(principal, run_id)  # WHY: a copy bound to this session

    @staticmethod
    def _require_owner_or_admin(principal: Principal, run: WorkflowRun) -> None:
        if run.user_id != principal.user_id and RoleName.ADMIN.value not in principal.roles:
            raise PermissionDeniedError("Only the owner or an admin can change this run")


def _dependents(plan: Plan, key: str) -> set[str]:
    """All steps that (transitively) depend on ``key``."""
    found: set[str] = set()
    frontier = {key}
    while frontier:
        nxt = {s.key for s in plan.steps if frontier & set(s.depends_on)} - found
        found |= nxt
        frontier = nxt
    return found


def timeline_totals(timeline: Timeline) -> dict[str, Any]:
    """Aggregate cost/usage numbers for the run header."""
    return {
        "llm_calls": len(timeline.llm_calls),
        "prompt_tokens": sum(c.prompt_tokens for c in timeline.llm_calls),
        "completion_tokens": sum(c.completion_tokens for c in timeline.llm_calls),
        "cost_usd": str(sum((c.cost_usd for c in timeline.llm_calls), start=0)),
        "tool_executions": len(timeline.tool_executions),
    }
