"""Refresh-token registry.

What: one row per issued refresh token (keyed by its JWT ``jti``).
Why: pure JWTs cannot be revoked. Tracking refresh tokens lets us (a) rotate on every use,
(b) revoke on logout, and (c) detect theft: if an already-used token is presented again,
someone has a copy, so the user's whole token family is revoked.
Connects to: ``services/auth_service.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    # WHY id == jti: lookup by the token's own identifier, no secret is stored in the DB.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    # WHY: "revoke / list all tokens of user X" and expiry clean-up.
    __table_args__ = (sa.Index("ix_refresh_tokens_user_id", "user_id"),)
