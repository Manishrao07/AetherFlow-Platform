"""Append-only audit log.

What: ``AuditLog`` rows record who did what, to which resource, when, from where.
Why append-only: an audit trail is only trustworthy if nobody - including a buggy
deployment - can edit or delete it. The migration installs DB triggers that reject
UPDATE, DELETE and TRUNCATE. (In production we would also grant the app's DB role only
INSERT/SELECT on this table.)
Why NO foreign keys: ``ON DELETE SET NULL`` would itself be an UPDATE (blocked by the
trigger), and the trail must outlive the user. So we store ``actor_id`` as a plain UUID plus
a snapshot of ``actor_email`` at the time of the action.
Connects to: ``core/audit.py`` (Phase 2), admin audit-log page.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, UUIDPrimaryKeyMixin


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_logs"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    action: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="success")
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    request_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.CheckConstraint("outcome IN ('success','failure','denied')", name="outcome_valid"),
        # WHY: admin page lists newest first.
        sa.Index("ix_audit_logs_created_at", sa.text("created_at DESC")),
        # WHY: "everything user X did".
        sa.Index("ix_audit_logs_actor_created", "actor_id", "created_at"),
        # WHY: "full history of this run / document / approval".
        sa.Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        sa.Index("ix_audit_logs_action_created", "action", "created_at"),
    )
