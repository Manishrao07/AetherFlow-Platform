"""Admin endpoints: user management and the audit log."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import ClientIpDep, UserServiceDep, require_permission_dep, require_roles_dep
from app.api.schemas.auth import ActiveUpdate, AuditLogOut, RolesUpdate, UserOut
from app.api.schemas.common import Page, PageParams, page_params
from app.core.rbac import Principal
from app.models.enums import RoleName
from app.services.permissions import AUDIT_READ

router = APIRouter(prefix="/api/v1", tags=["admin"])

AdminDep = Annotated[Principal, Depends(require_roles_dep(RoleName.ADMIN.value))]
AuditReaderDep = Annotated[Principal, Depends(require_permission_dep(AUDIT_READ))]
PageDep = Annotated[PageParams, Depends(page_params)]


@router.get("/users", response_model=Page[UserOut])
async def list_users(_: AdminDep, users: UserServiceDep, page: PageDep) -> Page[UserOut]:
    """List users (admin only)."""
    rows, total = await users.list_users(page)
    return Page(
        items=[UserOut.from_user(u) for u in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(user_id: uuid.UUID, _: AdminDep, users: UserServiceDep) -> UserOut:
    """Fetch one user (admin only)."""
    return UserOut.from_user(await users.get(user_id))


@router.put("/users/{user_id}/roles", response_model=UserOut)
async def set_roles(
    user_id: uuid.UUID, body: RolesUpdate, admin: AdminDep, users: UserServiceDep, ip: ClientIpDep
) -> UserOut:
    """Replace a user's roles (admin only, audited)."""
    return UserOut.from_user(await users.set_roles(admin, user_id, body.roles, ip))


@router.put("/users/{user_id}/active", response_model=UserOut)
async def set_active(
    user_id: uuid.UUID, body: ActiveUpdate, admin: AdminDep, users: UserServiceDep, ip: ClientIpDep
) -> UserOut:
    """Activate or deactivate an account (admin only, audited)."""
    return UserOut.from_user(await users.set_active(admin, user_id, body.is_active, ip))


@router.get("/audit-logs", response_model=Page[AuditLogOut])
async def list_audit_logs(
    _: AuditReaderDep,
    users: UserServiceDep,
    page: PageDep,
    action: Annotated[str | None, Query(max_length=100)] = None,
    actor_id: uuid.UUID | None = None,
    resource_type: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[AuditLogOut]:
    """Newest-first audit trail (read-only; there is deliberately no write/delete endpoint)."""
    rows, total = await users.list_audit_logs(page, action, actor_id, resource_type)
    return Page(
        items=[AuditLogOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )
