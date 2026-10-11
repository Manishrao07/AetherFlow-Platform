"""Workflow tables: runs, steps, step dependencies, tool executions, LLM calls.

What: the persisted state of every multi-agent run and the full execution trace.
Why: ALL orchestration state lives in Postgres (not memory) so a run can be resumed after a
crash and the UI timeline is just a query. ``depends_on`` is a real link table (3NF) so the
DB guarantees dependencies point at steps of the same run and can be indexed.
Connects to: the orchestrator (Phase 5) writes these; the timeline API reads them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_column
from app.models.enums import (
    AgentName,
    CallStatus,
    LLMCaller,
    ReviewVerdict,
    RiskLevel,
    RunMode,
    RunStatus,
    StepStatus,
)


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_runs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # WHY SET NULL: deleting/archiving a chat must not destroy the audit trail of runs.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    goal: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus, "run_status"),
        nullable=False,
        default=RunStatus.CREATED,
        server_default=RunStatus.CREATED.value,
    )
    # WHY JSONB: the plan is a validated but semi-structured document; the queryable parts
    # (steps, dependencies, status) are normalised into ``workflow_steps``.
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # WHY a column and not a flag inside ``plan``: the mode decides HOW the plan is made, so it
    # must be known (and survive revisions and restarts) before any plan exists.
    mode: Mapped[RunMode] = mapped_column(
        enum_column(RunMode, "run_mode"),
        nullable=False,
        default=RunMode.STANDARD,
        server_default=RunMode.STANDARD.value,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    revision_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    max_revisions: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="2")
    review_verdict: Mapped[ReviewVerdict | None] = mapped_column(
        enum_column(ReviewVerdict, "review_verdict"), nullable=True
    )
    review_score: Mapped[Decimal | None] = mapped_column(sa.Numeric(4, 3), nullable=True)
    # WHY: a client retrying "create workflow" after a network blip must not start two runs.
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    # WHY version column: optimistic locking - two workers/requests changing the same run
    # cannot silently overwrite each other; the loser gets StaleDataError and retries.
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # WHY a lease: exactly one worker may drive a run at a time. The owner renews the lease
    # while working; if the worker dies the lease expires and another worker takes over.
    lease_owner: Mapped[str | None] = mapped_column(sa.String(80), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012

    __table_args__ = (
        sa.CheckConstraint(
            "revision_count >= 0 AND revision_count <= max_revisions", name="revision_bounds"
        ),
        sa.CheckConstraint(
            "review_score IS NULL OR (review_score >= 0 AND review_score <= 1)",
            name="review_score_range",
        ),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="uq_workflow_runs_user_idempotency_key"
        ),
        # WHY: "my runs, newest first" (list endpoint, pagination).
        sa.Index("ix_workflow_runs_user_created", "user_id", sa.text("created_at DESC")),
        sa.Index("ix_workflow_runs_conversation_id", "conversation_id"),
        # WHY partial index: crash recovery scans only runs that SHOULD have a live worker;
        # terminal runs (the vast majority over time) are excluded, so it stays tiny.
        sa.Index(
            "ix_workflow_runs_recoverable",
            "status",
            "updated_at",
            postgresql_where=sa.text("status IN ('CREATED','PLANNING','EXECUTING','REVIEWING')"),
        ),
    )


class WorkflowStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_steps"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    # WHY step_key + position: the planner names steps ("s1", "s2"); position is the order.
    step_key: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # WHY revision: when the Reviewer asks for a revision the Planner makes a NEW plan; the old
    # steps stay in the table so the full history is visible in the timeline.
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    agent: Mapped[AgentName] = mapped_column(enum_column(AgentName, "step_agent"), nullable=False)
    tool: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[StepStatus] = mapped_column(
        enum_column(StepStatus, "step_status"),
        nullable=False,
        default=StepStatus.PENDING,
        server_default=StepStatus.PENDING.value,
    )
    risk_level: Mapped[RiskLevel] = mapped_column(
        enum_column(RiskLevel, "step_risk_level"),
        nullable=False,
        default=RiskLevel.LOW,
        server_default=RiskLevel.LOW.value,
    )
    requires_approval: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="3")
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.CheckConstraint("attempts >= 0 AND attempts <= max_attempts", name="attempts_bounds"),
        sa.CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        sa.CheckConstraint("revision >= 0", name="step_revision_non_negative"),
        sa.UniqueConstraint(
            "run_id", "revision", "step_key", name="uq_workflow_steps_run_revision_step_key"
        ),
        # WHY: timeline = "all steps of this run in order"; also the scheduler's main query.
        sa.Index("ix_workflow_steps_run_position", "run_id", "position"),
    )


class RunEvent(Base):
    """Append-only log of everything that happens to a run (transitions, retries, approvals).

    WHY a table: it is written in the SAME transaction as the state change, so the log can
    never disagree with the state, and the SSE endpoint just reads rows with ``id > last_seen``.
    That works with any number of API replicas (no in-memory pub/sub) and lets a reconnecting
    client resume exactly where it left off (``Last-Event-ID``).
    """

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False
    )

    __table_args__ = (sa.Index("ix_run_events_run_id_id", "run_id", "id"),)


class StepDependency(Base):
    """Edge of the plan DAG: ``step_id`` cannot start until ``depends_on_step_id`` succeeded."""

    __tablename__ = "step_dependencies"

    step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_steps.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_steps.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (
        sa.CheckConstraint("step_id <> depends_on_step_id", name="no_self_dependency"),
        # WHY: reverse lookup - "which steps become runnable when this one finishes?"
        sa.Index("ix_step_dependencies_depends_on", "depends_on_step_id"),
    )


class ToolExecution(UUIDPrimaryKeyMixin, Base):
    """One attempt of one tool call (inputs/outputs are stored already redacted)."""

    __tablename__ = "tool_executions"

    step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_steps.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # WHY unique idempotency key: "step X, attempt N" can only be recorded once, so a
    # replayed job after a crash cannot repeat a side effect (e.g. send the email twice).
    idempotency_key: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[CallStatus] = mapped_column(
        enum_column(CallStatus, "tool_call_status"), nullable=False
    )
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.CheckConstraint("attempt >= 1", name="attempt_positive"),
        sa.UniqueConstraint("idempotency_key", name="uq_tool_executions_idempotency_key"),
        sa.Index("ix_tool_executions_step_id", "step_id"),
        # WHY: tool failure-rate analytics ("which tool fails most this week?").
        sa.Index("ix_tool_executions_tool_status_created", "tool_name", "status", "created_at"),
    )


class LLMCall(UUIDPrimaryKeyMixin, Base):
    """One LLM request: model, tokens, latency and estimated cost (cost tracking + analytics)."""

    __tablename__ = "llm_calls"

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=True
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_steps.id", ondelete="SET NULL"), nullable=True
    )
    agent: Mapped[LLMCaller] = mapped_column(
        enum_column(LLMCaller, "llm_call_agent"), nullable=False
    )
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    model: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6), nullable=False, server_default="0")
    cached: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    status: Mapped[CallStatus] = mapped_column(
        enum_column(CallStatus, "llm_call_status"), nullable=False
    )
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND latency_ms >= 0 AND cost_usd >= 0",
            name="usage_non_negative",
        ),
        sa.Index("ix_llm_calls_run_created", "run_id", "created_at"),
        sa.Index("ix_llm_calls_step_id", "step_id"),
        # WHY: cost / token dashboards aggregate by time window.
        sa.Index("ix_llm_calls_created_at", "created_at"),
    )
