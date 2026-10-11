"""Human-in-the-loop approvals.

What: ``Approval`` - a pending/decided request for a human to allow a plan or a risky step.
Why: approvals are durable records (not in-memory flags) so a paused run survives restarts
and every decision is attributable. Separation of duties is enforced twice: in service code
AND by a DB CHECK constraint, so a bug in the API layer still cannot self-approve.
Connects to: orchestrator/approvals.py (Phase 5), approvals inbox in the UI.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_column
from app.models.enums import ApprovalKind, ApprovalStatus, RiskLevel


class Approval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approvals"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_steps.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[ApprovalKind] = mapped_column(
        enum_column(ApprovalKind, "approval_kind"), nullable=False
    )
    status: Mapped[ApprovalStatus] = mapped_column(
        enum_column(ApprovalStatus, "approval_status"),
        nullable=False,
        default=ApprovalStatus.PENDING,
        server_default=ApprovalStatus.PENDING.value,
    )
    risk_level: Mapped[RiskLevel] = mapped_column(
        enum_column(RiskLevel, "approval_risk_level"), nullable=False
    )
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # WHY RESTRICT: a user who requested an approval cannot be deleted away from history.
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    comment: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Separation of duties, enforced by the database itself (defence in depth).
        sa.CheckConstraint(
            "decided_by IS NULL OR decided_by <> requested_by", name="separation_of_duties"
        ),
        # A decision must record who and when.
        sa.CheckConstraint(
            "status NOT IN ('APPROVED','REJECTED') "
            "OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="decision_complete",
        ),
        sa.Index("ix_approvals_run_id", "run_id"),
        sa.Index("ix_approvals_step_id", "step_id"),
        # WHY partial index: the approvals inbox and the expiry sweeper only look at PENDING
        # rows, which are a tiny fraction of the table once history accumulates.
        sa.Index(
            "ix_approvals_pending_expires",
            "expires_at",
            postgresql_where=sa.text("status = 'PENDING'"),
        ),
    )
