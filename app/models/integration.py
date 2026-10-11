"""Tables written by side-effecting tools: tickets and the notification outbox.

What: ``Ticket`` (our mocked external ticketing system) and ``NotificationOutbox`` (our mocked
email sender - rows are "sent" by being stored).
Why mocks as real tables: the demo and tests can show and verify side effects; the unique
``idempotency_key`` on both means a retried or replayed tool call can never create a second
ticket or send a second email, even if the application crashed mid-way.
Connects to: ``tools/builtin/create_ticket.py`` and ``send_notification.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, UUIDPrimaryKeyMixin


class Ticket(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "tickets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    severity: Mapped[str] = mapped_column(sa.String(8), nullable=False, server_default="SEV3")
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="OPEN")
    idempotency_key: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.CheckConstraint("severity IN ('SEV1','SEV2','SEV3','SEV4')", name="severity_valid"),
        sa.CheckConstraint("status IN ('OPEN','CLOSED')", name="status_valid"),
        sa.UniqueConstraint("idempotency_key", name="uq_tickets_idempotency_key"),
        sa.Index("ix_tickets_user_created", "user_id", "created_at"),
        sa.Index("ix_tickets_run_id", "run_id"),
    )


class NotificationOutbox(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "notification_outbox"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(sa.String(16), nullable=False, server_default="email")
    recipient: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    subject: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint("idempotency_key", name="uq_notification_outbox_idempotency_key"),
        sa.Index("ix_notification_outbox_user_created", "user_id", "created_at"),
        sa.Index("ix_notification_outbox_run_id", "run_id"),
    )
