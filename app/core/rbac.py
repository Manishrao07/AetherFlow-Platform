"""Role-based access control primitives (framework-agnostic).

What: ``Principal`` (the authenticated caller), ``PermissionMap`` (role -> permissions) and
``require_roles`` / ``require_permission`` checks.
Why two levels: routers usually ask "is this user an approver?" (roles), while tools ask
"may this caller use the notification tool?" (permissions). A role->permission map keeps
that mapping in ONE table instead of scattered ``if role == ...`` checks.
Connects to: ``api/deps.py`` (FastAPI wiring) and the tool registry (Phase 3) which calls
``PermissionMap.allows`` before running any tool.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.core.errors import PermissionDeniedError


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, rebuilt from the database on every request."""

    user_id: uuid.UUID
    email: str
    roles: frozenset[str]

    def has_any_role(self, allowed: Iterable[str]) -> bool:
        return bool(self.roles & frozenset(allowed))


class PermissionMap:
    """Immutable role -> permission lookup."""

    def __init__(self, mapping: Mapping[str, Iterable[str]]) -> None:
        self._mapping = {role: frozenset(perms) for role, perms in mapping.items()}

    def permissions_for(self, roles: Iterable[str]) -> frozenset[str]:
        granted: set[str] = set()
        for role in roles:
            granted |= self._mapping.get(role, frozenset())
        return frozenset(granted)

    def allows(self, roles: Iterable[str], permission: str) -> bool:
        return permission in self.permissions_for(roles)


def require_roles(principal: Principal, *allowed: str) -> None:
    """Raise 403 unless the principal holds at least one allowed role."""
    if not principal.has_any_role(allowed):
        raise PermissionDeniedError(
            "You do not have permission to perform this action",
            details={"required_any_of": sorted(allowed)},
        )


def require_permission(principal: Principal, permissions: PermissionMap, permission: str) -> None:
    """Raise 403 unless one of the principal's roles grants ``permission``."""
    if not permissions.allows(principal.roles, permission):
        raise PermissionDeniedError(
            "You do not have permission to perform this action",
            details={"required_permission": permission},
        )
