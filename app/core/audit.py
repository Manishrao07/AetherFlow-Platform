"""Append-only audit trail writer.

What: ``AuditService.record(...)`` inserts one row into ``audit_logs`` using its OWN short
transaction.
Why own transaction: audit entries must survive even when the surrounding request fails and
rolls back - a failed login or a denied action is exactly what we most need to keep.
Why Core table (not an ORM model import): this module lives in ``core/`` and must not depend
on Project 1 models, so Project 2 can reuse it with the same table shape.
Why redact: ``details`` are stored forever; secrets must never reach them.
Connects to: auth/user services and RBAC denials; read back by the admin audit endpoint.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_log_context
from app.core.redaction import redact

logger = logging.getLogger(__name__)

# Lightweight table definition matching migration 0001 (insert-only use).
_audit_logs = sa.table(
    "audit_logs",
    sa.column("actor_id", PG_UUID(as_uuid=True)),
    sa.column("actor_email", sa.String),
    sa.column("action", sa.String),
    sa.column("resource_type", sa.String),
    sa.column("resource_id", sa.String),
    sa.column("outcome", sa.String),
    sa.column("details", JSONB),
    sa.column("ip_address", INET),
    sa.column("request_id", sa.String),
)


class AuditService:
    """Writes audit entries. Never raises into the caller's business flow."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        action: str,
        *,
        actor_id: uuid.UUID | None = None,
        actor_email: str | None = None,
        resource_type: str | None = None,
        resource_id: str | uuid.UUID | None = None,
        outcome: str = "success",
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> None:
        """Persist one audit entry (outcome: success | failure | denied)."""
        row = {
            "actor_id": actor_id,
            "actor_email": actor_email,
            "action": action,
            "resource_type": resource_type,
            "resource_id": str(resource_id) if resource_id is not None else None,
            "outcome": outcome,
            "details": redact(details or {}),
            "ip_address": ip_address,
            "request_id": get_log_context().get("request_id"),
        }
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(sa.insert(_audit_logs).values(**row))
        except Exception:
            # WHY swallow+log: an audit-storage hiccup must not turn a successful user action
            # into a 500; it is logged loudly so operators notice.
            logger.exception("failed to write audit log", extra={"audit_action": action})
