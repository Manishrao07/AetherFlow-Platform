"""Identity tables: users, roles and the user<->role link.

What: ``User``, ``Role`` and ``UserRole`` ORM models.
Why: roles live in their own table with a many-to-many link (3NF; a user may hold several
roles, e.g. operator + approver) instead of a comma-separated string on the user row.
Connects to: auth service / RBAC (Phase 2); every other table points at ``users``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Role(UUIDPrimaryKeyMixin, Base):
    """A named permission group. Rows are inserted by the migration (reference data)."""

    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(sa.String(32), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    full_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())

    # WHY explicit joins: user_roles has TWO foreign keys to users (user_id and granted_by),
    # so SQLAlchemy cannot guess which one means "the user who holds the role".
    # WHY selectin: roles are needed on every authenticated request; one extra query beats N+1.
    roles: Mapped[list[Role]] = relationship(
        secondary="user_roles",
        primaryjoin="User.id == UserRole.user_id",
        secondaryjoin="Role.id == UserRole.role_id",
        lazy="selectin",
    )

    __table_args__ = (
        # WHY functional unique index: "Bob@x.com" and "bob@x.com" are the same person.
        # Also serves the login lookup (WHERE lower(email) = :email).
        sa.Index("uq_users_email_lower", sa.func.lower(email), unique=True),
    )


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True
    )
    # WHY: role changes are audited; knowing who granted it makes the audit trail complete.
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    # WHY index on role_id: the PK covers user->roles; this covers "all users with role X".
    __table_args__ = (sa.Index("ix_user_roles_role_id", "role_id"),)
