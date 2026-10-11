"""RBAC primitives and the application's permission table."""

import uuid

import pytest

from app.core.errors import PermissionDeniedError
from app.core.rbac import PermissionMap, Principal, require_permission, require_roles
from app.services import permissions as perm
from app.services.permissions import PERMISSIONS


def principal(*roles: str) -> Principal:
    return Principal(uuid.uuid4(), "u@test.dev", frozenset(roles))


def test_require_roles() -> None:
    require_roles(principal("operator"), "operator", "admin")
    with pytest.raises(PermissionDeniedError) as exc:
        require_roles(principal("viewer"), "admin")
    assert exc.value.status_code == 403 and exc.value.details == {"required_any_of": ["admin"]}


def test_no_roles_means_no_access() -> None:
    assert not principal().has_any_role(["admin"])
    with pytest.raises(PermissionDeniedError):
        require_roles(principal(), "viewer")


def test_permission_map_unions_roles_and_ignores_unknown() -> None:
    pmap = PermissionMap({"a": {"x"}, "b": {"y"}})
    assert pmap.permissions_for(["a", "b", "nope"]) == {"x", "y"}
    assert pmap.allows(["a"], "x") and not pmap.allows(["a"], "y")
    require_permission(principal("a"), pmap, "x")
    with pytest.raises(PermissionDeniedError):
        require_permission(principal("a"), pmap, "y")


@pytest.mark.parametrize(
    ("role", "permission", "expected"),
    [
        ("viewer", perm.WORKFLOW_READ, True),
        ("viewer", perm.WORKFLOW_CREATE, False),
        ("viewer", perm.APPROVAL_DECIDE, False),
        ("operator", perm.WORKFLOW_CREATE, True),
        ("operator", perm.APPROVAL_DECIDE, False),
        ("operator", perm.USER_MANAGE, False),
        ("approver", perm.APPROVAL_DECIDE, True),
        ("approver", perm.USER_MANAGE, False),
        ("approver", perm.AUDIT_READ, False),
        ("admin", perm.USER_MANAGE, True),
        ("admin", perm.AUDIT_READ, True),
        ("admin", perm.APPROVAL_DECIDE, True),
    ],
)
def test_permission_matrix(role: str, permission: str, expected: bool) -> None:
    assert PERMISSIONS.allows([role], permission) is expected
