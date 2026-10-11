"""Admin user management and audit-log queries.

What: list/get users, replace roles, activate/deactivate, and page through the audit log.
Why lock-out guards: an admin must not be able to remove their own admin role or disable
themselves, otherwise one misclick could leave the system with nobody able to administer it.
Every change is written to the audit log (who changed whom, from what to what).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.common import PageParams
from app.core.audit import AuditService
from app.core.errors import ConflictError, NotFoundError
from app.core.rbac import Principal
from app.models import AuditLog, RefreshToken, Role, User, UserRole
from app.models.enums import RoleName


class UserService:
    def __init__(self, session: AsyncSession, audit: AuditService) -> None:
        self._session = session
        self._audit = audit

    async def list_users(self, page: PageParams) -> tuple[list[User], int]:
        total = await self._session.scalar(sa.select(sa.func.count()).select_from(User)) or 0
        rows = await self._session.scalars(
            sa.select(User).order_by(User.created_at, User.id).limit(page.limit).offset(page.offset)
        )
        return list(rows), total

    async def get(self, user_id: uuid.UUID) -> User:
        user = await self._session.get(User, user_id)
        if user is None:
            raise NotFoundError("User not found")
        return user

    async def set_roles(
        self, actor: Principal, user_id: uuid.UUID, roles: list[RoleName], ip: str | None
    ) -> User:
        """Replace a user's roles."""
        user = await self.get(user_id)
        wanted = {r.value for r in roles}
        if user.id == actor.user_id and RoleName.ADMIN.value not in wanted:
            raise ConflictError("You cannot remove your own admin role")
        before = sorted(role.name for role in user.roles)
        rows = await self._session.scalars(sa.select(Role).where(Role.name.in_(wanted)))
        new_roles = list(rows)
        await self._session.execute(sa.delete(UserRole).where(UserRole.user_id == user.id))
        self._session.add_all(
            UserRole(user_id=user.id, role_id=role.id, granted_by=actor.user_id)
            for role in new_roles
        )
        await self._session.flush()
        await self._session.refresh(user, attribute_names=["roles"])
        await self._audit.record(
            "user.roles_changed", actor_id=actor.user_id, actor_email=actor.email,
            resource_type="user", resource_id=user.id,
            details={"before": before, "after": sorted(wanted)}, ip_address=ip,
        )  # fmt: skip
        return user

    async def set_active(
        self, actor: Principal, user_id: uuid.UUID, is_active: bool, ip: str | None
    ) -> User:
        """Enable/disable an account; disabling also kills every refresh token."""
        user = await self.get(user_id)
        if user.id == actor.user_id and not is_active:
            raise ConflictError("You cannot deactivate your own account")
        user.is_active = is_active
        if not is_active:
            await self._session.execute(
                sa.update(RefreshToken)
                .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=sa.func.now())
            )
        await self._session.flush()
        await self._audit.record(
            "user.activated" if is_active else "user.deactivated",
            actor_id=actor.user_id, actor_email=actor.email,
            resource_type="user", resource_id=user.id, ip_address=ip,
        )  # fmt: skip
        return user

    async def list_audit_logs(
        self,
        page: PageParams,
        action: str | None = None,
        actor_id: uuid.UUID | None = None,
        resource_type: str | None = None,
    ) -> tuple[list[AuditLog], int]:
        """Newest-first audit entries with optional filters (served by the audit indexes)."""
        conditions = []
        if action:
            conditions.append(AuditLog.action == action)
        if actor_id:
            conditions.append(AuditLog.actor_id == actor_id)
        if resource_type:
            conditions.append(AuditLog.resource_type == resource_type)
        total = (
            await self._session.scalar(
                sa.select(sa.func.count()).select_from(AuditLog).where(*conditions)
            )
            or 0
        )
        rows = await self._session.scalars(
            sa.select(AuditLog)
            .where(*conditions)
            .order_by(AuditLog.created_at.desc(), AuditLog.id)
            .limit(page.limit)
            .offset(page.offset)
        )
        return list(rows), total
