"""RunStore: the only code that changes run/step status in the database.

What: small, transactional operations - ``transition_run``, ``transition_step``, event
emission, lease claim/renew/release and orphan recovery.
Why a dedicated store: every status change must (1) be validated by the state machine,
(2) be written together with its ``run_events`` row in ONE transaction, and (3) survive
concurrent writers. Centralising that means the engine cannot forget any of the three.
Concurrency: runs use optimistic locking (``version`` column). If two writers collide (e.g. the
engine moving a run while a user cancels it) the loser reloads and re-checks; if the run has
meanwhile reached a terminal state, ``RunInterrupted`` tells the engine to stop quietly.
Leases: a run is driven by one worker at a time. The worker's lease is renewed while it works; a
dead worker's lease simply expires, which is how "runs that lost their worker" are detected.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app.core.errors import NotFoundError
from app.metrics import WORKFLOW_DURATION, WORKFLOW_RUNS
from app.models import RunEvent, WorkflowRun, WorkflowStep
from app.models.enums import RunStatus, StepStatus
from app.orchestrator.state_machine import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    InvalidTransitionError,
    assert_run_transition,
    assert_step_transition,
)

logger = logging.getLogger(__name__)

_MAX_CONFLICT_RETRIES = 5
Extra = Callable[[AsyncSession, WorkflowRun], Awaitable[None]]


class RunInterrupted(Exception):
    """The run is no longer ours to drive (cancelled, or already finished by someone else)."""

    def __init__(self, status: RunStatus) -> None:
        super().__init__(f"run is {status.value}")
        self.status = status


def add_event(session: AsyncSession, run_id: uuid.UUID, type_: str, data: dict[str, Any]) -> None:
    """Queue an event row in the caller's transaction."""
    session.add(RunEvent(run_id=run_id, type=type_, data=data))


class RunStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------ reads
    async def get_run(self, run_id: uuid.UUID) -> WorkflowRun:
        async with self._sf() as session:
            run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise NotFoundError("Workflow run not found")
        return run

    async def get_steps(self, run_id: uuid.UUID, revision: int | None = None) -> list[WorkflowStep]:
        stmt = sa.select(WorkflowStep).where(WorkflowStep.run_id == run_id)
        if revision is not None:
            stmt = stmt.where(WorkflowStep.revision == revision)
        async with self._sf() as session:
            rows = await session.scalars(
                stmt.order_by(WorkflowStep.revision, WorkflowStep.position)
            )
            return list(rows)

    # ------------------------------------------------------------- run status
    async def transition_run(
        self,
        run_id: uuid.UUID,
        target: RunStatus,
        *,
        reason: str | None = None,
        extra: Extra | None = None,
        **fields: Any,
    ) -> WorkflowRun:
        """Move the run to ``target`` (validated), write the event, apply ``fields``.

        ``extra`` runs inside the same transaction (e.g. create the Approval row), so the
        status and its companion data change together or not at all.
        """
        for _ in range(_MAX_CONFLICT_RETRIES):
            try:
                async with self._sf() as session, session.begin():
                    run = await session.get(WorkflowRun, run_id)
                    if run is None:
                        raise NotFoundError("Workflow run not found")
                    previous = run.status
                    try:
                        assert_run_transition(previous, target)
                    except InvalidTransitionError:
                        if previous in TERMINAL_RUN_STATUSES:
                            raise RunInterrupted(previous) from None
                        raise
                    run.status = target
                    for name, value in fields.items():
                        setattr(run, name, value)
                    now = datetime.now(UTC)
                    if run.started_at is None and target is not RunStatus.CREATED:
                        run.started_at = now
                    if target in TERMINAL_RUN_STATUSES:
                        run.finished_at = now
                    elif previous in TERMINAL_RUN_STATUSES:  # re-opened by a retry-step
                        run.finished_at = None
                    add_event(
                        session, run_id, "run.status",
                        {"from": previous.value, "to": target.value, "reason": reason},
                    )  # fmt: skip
                    if extra is not None:
                        await extra(session, run)
                    await session.flush()
                    seconds = (now - run.created_at).total_seconds()
            except StaleDataError:
                logger.info("run update conflict; reloading", extra={"run_id": str(run_id)})
                continue
            # WHY after the commit: a rolled-back transition must not be counted.
            if target in TERMINAL_RUN_STATUSES:
                WORKFLOW_RUNS.labels(target.value).inc()
                WORKFLOW_DURATION.labels(target.value).observe(seconds)
            return run
        raise RuntimeError(f"could not update run {run_id}: too many concurrent updates")

    # ------------------------------------------------------------ step status
    async def transition_step(
        self, step_id: uuid.UUID, target: StepStatus, *, reason: str | None = None, **fields: Any
    ) -> WorkflowStep:
        async with self._sf() as session, session.begin():
            step = await session.get(WorkflowStep, step_id)
            if step is None:
                raise NotFoundError("Workflow step not found")
            previous = step.status
            assert_step_transition(previous, target)
            step.status = target
            for name, value in fields.items():
                setattr(step, name, value)
            now = datetime.now(UTC)
            if target is StepStatus.RUNNING:
                step.started_at = now
                step.finished_at = None
            if target in {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED}:
                step.finished_at = now
            add_event(
                session, step.run_id, "step.status",
                {
                    "step_id": str(step_id), "key": step.step_key, "revision": step.revision,
                    "from": previous.value, "to": target.value, "attempt": step.attempts,
                    "reason": reason,
                },
            )  # fmt: skip
            await session.flush()
            return step

    async def emit(self, run_id: uuid.UUID, type_: str, data: dict[str, Any]) -> None:
        """Write a stand-alone event (retry notices, review verdicts, ...)."""
        async with self._sf() as session, session.begin():
            add_event(session, run_id, type_, data)

    # ------------------------------------------------------------------ leases
    async def claim(self, run_id: uuid.UUID, owner: str, ttl_seconds: float) -> bool:
        """Atomically take the lease if nobody holds a live one."""
        async with self._sf() as session, session.begin():
            result = await session.execute(
                sa.update(WorkflowRun)
                .where(
                    WorkflowRun.id == run_id,
                    WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
                    sa.or_(
                        WorkflowRun.lease_expires_at.is_(None),
                        WorkflowRun.lease_expires_at < sa.func.now(),
                    ),
                )
                .values(
                    lease_owner=owner,
                    lease_expires_at=sa.func.now()
                    + sa.func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds),
                )
                .returning(WorkflowRun.id)
            )
            return result.scalar_one_or_none() is not None

    async def renew(self, run_id: uuid.UUID, owner: str, ttl_seconds: float) -> bool:
        async with self._sf() as session, session.begin():
            result = await session.execute(
                sa.update(WorkflowRun)
                .where(WorkflowRun.id == run_id, WorkflowRun.lease_owner == owner)
                .values(
                    lease_expires_at=sa.func.now()
                    + sa.func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds)
                )
                .returning(WorkflowRun.id)
            )
            return result.scalar_one_or_none() is not None

    async def release(self, run_id: uuid.UUID, owner: str) -> None:
        async with self._sf() as session, session.begin():
            await session.execute(
                sa.update(WorkflowRun)
                .where(WorkflowRun.id == run_id, WorkflowRun.lease_owner == owner)
                .values(lease_owner=None, lease_expires_at=None)
            )

    # ---------------------------------------------------------------- recovery
    async def find_orphans(self, limit: int = 100) -> list[uuid.UUID]:
        """Active runs whose lease is missing or expired (their worker is gone)."""
        async with self._sf() as session:
            rows = await session.scalars(
                sa.select(WorkflowRun.id)
                .where(
                    WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
                    sa.or_(
                        WorkflowRun.lease_expires_at.is_(None),
                        WorkflowRun.lease_expires_at < sa.func.now(),
                    ),
                )
                .order_by(WorkflowRun.updated_at)
                .limit(limit)
            )
            return list(rows)

    async def reset_interrupted_steps(self, run_id: uuid.UUID) -> int:
        """Put steps that were RUNNING when the worker died back to PENDING.

        WHY ``attempts - 1``: the next try re-uses the SAME attempt number, hence the same
        idempotency key, so if the tool had already finished the executor replays the stored
        result instead of running it (and, say, sending the email) a second time.
        """
        async with self._sf() as session, session.begin():
            steps = (
                await session.scalars(
                    sa.select(WorkflowStep).where(
                        WorkflowStep.run_id == run_id, WorkflowStep.status == StepStatus.RUNNING
                    )
                )
            ).all()
            for step in steps:
                step.status = StepStatus.PENDING
                step.attempts = max(step.attempts - 1, 0)
                add_event(
                    session, run_id, "step.status",
                    {"step_id": str(step.id), "key": step.step_key, "revision": step.revision,
                     "from": "RUNNING", "to": "PENDING", "reason": "recovered after worker loss"},
                )  # fmt: skip
            return len(steps)
