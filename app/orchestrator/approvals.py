"""Human-in-the-loop approvals: request, decide, expire.

What: ``request_plan_approval`` (called by the engine inside the transition to
AWAITING_APPROVAL), and ``ApprovalService`` for the API: list, approve, reject, expire.
Rules enforced here (and, for the first two, again by DB constraints):
 - only roles with ``approval:decide`` (approver, admin) may decide;
 - separation of duties: whoever requested the run cannot approve it, even an admin;
 - a decision is final and atomic: ``UPDATE ... WHERE status='PENDING'`` means two approvers
   clicking at once produce exactly one decision;
 - approvals expire: a plan nobody looks at must not wait forever or run days later on stale
   assumptions.
Why approve the whole PLAN rather than each step: the approver reviews the exact tools and
inputs that will run (shown in the inbox). Once approved, steps flagged ``requires_approval``
may execute; nothing else changes about what runs.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.plan import Plan
from app.api.schemas.common import PageParams
from app.core.audit import AuditService
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.rbac import Principal
from app.metrics import APPROVAL_DECISIONS, APPROVAL_WAIT
from app.models import Approval, User, WorkflowRun
from app.models.enums import ApprovalKind, ApprovalStatus, RoleName, RunStatus
from app.orchestrator.store import RunInterrupted, RunStore, add_event
from app.services.permissions import APPROVAL_DECIDE, PERMISSIONS

logger = logging.getLogger(__name__)

EnqueueFn = Callable[[uuid.UUID], Awaitable[None]]
_REVIEWER_ROLES = {RoleName.ADMIN.value, RoleName.APPROVER.value}


def describe_plan_for_approver(plan: Plan) -> str:
    """Plain-text reason shown to the approver: which steps need sign-off and why."""
    gated = [s for s in plan.steps if s.requires_approval]
    lines = [f"Plan: {plan.summary}", f"Highest risk: {plan.max_risk.value}"]
    lines += [f"- {s.key}: {s.description} [{s.tool}, risk {s.risk_level.value}]" for s in gated]
    return "\n".join(lines)


async def request_plan_approval(
    session: AsyncSession, run: WorkflowRun, plan: Plan, ttl_seconds: int
) -> Approval:
    """Create the PENDING approval for ``plan`` (call inside the status transition)."""
    approval = Approval(
        run_id=run.id,
        kind=ApprovalKind.PLAN,
        risk_level=plan.max_risk,
        reason=describe_plan_for_approver(plan),
        requested_by=run.user_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
    )
    session.add(approval)
    await session.flush()
    add_event(
        session, run.id, "approval.requested",
        {"approval_id": str(approval.id), "risk": plan.max_risk.value,
         "expires_at": approval.expires_at.isoformat()},
    )  # fmt: skip
    return approval


@dataclass(frozen=True)
class ApprovalView:
    """An approval plus what the approver needs to see in order to decide."""

    approval: Approval
    goal: str
    plan: dict[str, Any] | None
    requester_email: str


def _view_query() -> sa.Select[tuple[Approval, str, dict[str, Any] | None, str]]:
    return (
        sa.select(Approval, WorkflowRun.goal, WorkflowRun.plan, User.email)
        .join(WorkflowRun, WorkflowRun.id == Approval.run_id)
        .join(User, User.id == Approval.requested_by)
    )


class ApprovalService:
    def __init__(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        store: RunStore,
        audit: AuditService,
        enqueue: EnqueueFn,
    ) -> None:
        self._session = session
        self._sf = session_factory
        self._store = store
        self._audit = audit
        self._enqueue = enqueue

    # ----------------------------------------------------------------- queries
    async def list_for(
        self, principal: Principal, status: ApprovalStatus | None, page: PageParams
    ) -> tuple[list[ApprovalView], int]:
        """Approvers/admins see every approval; everyone else sees only their own requests."""
        conditions = []
        if status is not None:
            conditions.append(Approval.status == status)
        if not principal.has_any_role(_REVIEWER_ROLES):
            conditions.append(Approval.requested_by == principal.user_id)
        total = (
            await self._session.scalar(
                sa.select(sa.func.count()).select_from(Approval).where(*conditions)
            )
            or 0
        )
        rows = await self._session.execute(
            _view_query()
            .where(*conditions)
            .order_by(Approval.created_at.desc(), Approval.id)
            .limit(page.limit)
            .offset(page.offset)
        )
        return [ApprovalView(*row) for row in rows.all()], total

    async def get(self, principal: Principal, approval_id: uuid.UUID) -> ApprovalView:
        row = (await self._session.execute(_view_query().where(Approval.id == approval_id))).first()
        if row is None:
            raise NotFoundError("Approval not found")
        view = ApprovalView(*row)
        if not (
            principal.has_any_role(_REVIEWER_ROLES)
            or view.approval.requested_by == principal.user_id
        ):
            raise NotFoundError("Approval not found")
        return view

    # --------------------------------------------------------------- decisions
    async def decide(
        self,
        principal: Principal,
        approval_id: uuid.UUID,
        *,
        approve: bool,
        comment: str | None,
        ip: str | None = None,
    ) -> Approval:
        if not PERMISSIONS.allows(principal.roles, APPROVAL_DECIDE):
            raise PermissionDeniedError("Only approvers and admins can decide approvals")
        approval = await self._session.get(Approval, approval_id)
        if approval is None:
            raise NotFoundError("Approval not found")
        if approval.requested_by == principal.user_id:
            raise PermissionDeniedError(
                "You cannot decide an approval for your own workflow (separation of duties)"
            )
        if approval.status is not ApprovalStatus.PENDING:
            raise ConflictError(f"Approval is already {approval.status.value}")
        if approval.expires_at <= datetime.now(UTC):
            await self.expire_overdue()
            raise ConflictError("Approval has expired")

        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        # WHY a conditional UPDATE: compare-and-set in the database, so concurrent deciders
        # cannot both succeed.
        decided = await self._session.execute(
            sa.update(Approval)
            .where(Approval.id == approval_id, Approval.status == ApprovalStatus.PENDING)
            .values(
                status=status, decided_by=principal.user_id, decided_at=sa.func.now(),
                comment=comment,
            )
            .returning(Approval.id)
        )  # fmt: skip
        if decided.scalar_one_or_none() is None:
            raise ConflictError("Approval was just decided by someone else")
        await self._session.commit()
        await self._session.refresh(approval)
        decision = status.value.lower()
        APPROVAL_DECISIONS.labels(decision).inc()
        if approval.decided_at is not None:
            wait = (approval.decided_at - approval.created_at).total_seconds()
            APPROVAL_WAIT.labels(decision).observe(wait)

        await self._advance_run(approval, approve, comment, principal)
        await self._audit.record(
            "approval.approved" if approve else "approval.rejected",
            actor_id=principal.user_id, actor_email=principal.email,
            resource_type="approval", resource_id=approval_id, ip_address=ip,
            details={"run_id": str(approval.run_id), "risk": approval.risk_level.value,
                     "comment": comment},
        )  # fmt: skip
        return approval

    async def _advance_run(
        self, approval: Approval, approve: bool, comment: str | None, principal: Principal
    ) -> None:
        """Resume (approved) or cancel (rejected) the run that was waiting."""
        await self._store.emit(
            approval.run_id, "approval.decided",
            {"approval_id": str(approval.id), "approved": approve, "by": principal.email,
             "comment": comment},
        )  # fmt: skip
        try:
            if approve:
                await self._store.transition_run(
                    approval.run_id, RunStatus.EXECUTING, reason=f"approved by {principal.email}"
                )
                await self._enqueue(approval.run_id)
            else:
                await self._store.transition_run(
                    approval.run_id, RunStatus.CANCELLED,
                    reason=f"rejected by {principal.email}",
                    error=f"Plan rejected by {principal.email}: {comment or 'no comment'}",
                )  # fmt: skip
        except RunInterrupted:
            logger.info("approval decided but run already finished",
                        extra={"run_id": str(approval.run_id)})  # fmt: skip

    async def expire_overdue(self) -> int:
        return await expire_overdue(self._sf, self._store)


async def expire_overdue(session_factory: async_sessionmaker[AsyncSession], store: RunStore) -> int:
    """Mark overdue PENDING approvals EXPIRED and fail their runs. Returns the count."""
    async with session_factory() as session, session.begin():
        expired = (
            await session.execute(
                sa.update(Approval)
                .where(
                    Approval.status == ApprovalStatus.PENDING, Approval.expires_at <= sa.func.now()
                )
                .values(status=ApprovalStatus.EXPIRED)
                .returning(Approval.id, Approval.run_id, Approval.created_at)
            )
        ).all()
    for approval_id, run_id, created_at in expired:
        APPROVAL_DECISIONS.labels("expired").inc()
        APPROVAL_WAIT.labels("expired").observe((datetime.now(UTC) - created_at).total_seconds())
        await store.emit(run_id, "approval.expired", {"approval_id": str(approval_id)})
        try:
            await store.transition_run(
                run_id,
                RunStatus.FAILED,
                reason="approval expired",
                error="Approval expired before anyone decided",
            )
        except RunInterrupted:
            continue
    return len(expired)


async def approval_views_for_run(session: AsyncSession, run_id: uuid.UUID) -> list[ApprovalView]:
    """All approvals of one run, oldest first (used by the timeline)."""
    rows = await session.execute(
        _view_query().where(Approval.run_id == run_id).order_by(Approval.created_at)
    )
    return [ApprovalView(*row) for row in rows.all()]


async def plan_is_approved(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> bool:
    """True when the MOST RECENT plan approval of the run was granted."""
    async with session_factory() as session:
        latest = await session.scalar(
            sa.select(Approval.status)
            .where(Approval.run_id == run_id, Approval.kind == ApprovalKind.PLAN)
            .order_by(Approval.created_at.desc())
            .limit(1)
        )
    return latest is ApprovalStatus.APPROVED
