"""The single role -> permission table for the whole application.

What: permission name constants and ``PERMISSIONS`` (a ``PermissionMap``).
Why one table: "who may do what" must be reviewable in one screen. Routers and the tool
registry both ask this map, so they can never disagree.
Least privilege: viewer is read-only; operator can run work; approver can additionally decide
approvals; admin manages users and reads the audit log.
"""

from __future__ import annotations

from app.core.rbac import PermissionMap
from app.models.enums import RoleName

# --- permission names -------------------------------------------------------
WORKFLOW_READ = "workflow:read"
WORKFLOW_CREATE = "workflow:create"
WORKFLOW_CANCEL = "workflow:cancel"
APPROVAL_READ = "approval:read"
APPROVAL_DECIDE = "approval:decide"
DOCUMENT_READ = "document:read"
DOCUMENT_WRITE = "document:write"
CONVERSATION_READ = "conversation:read"
CONVERSATION_WRITE = "conversation:write"
MEMORY_READ = "memory:read"
MEMORY_WRITE = "memory:write"
TICKET_CREATE = "ticket:create"
NOTIFICATION_SEND = "notification:send"
HTTP_FETCH = "tool:http_fetch"
RESEARCH_SEARCH = "tool:research_search"
USER_MANAGE = "user:manage"
AUDIT_READ = "audit:read"

_READ_ONLY = {WORKFLOW_READ, APPROVAL_READ, DOCUMENT_READ, CONVERSATION_READ, MEMORY_READ}
_OPERATE = _READ_ONLY | {
    WORKFLOW_CREATE,
    WORKFLOW_CANCEL,
    DOCUMENT_WRITE,
    CONVERSATION_WRITE,
    MEMORY_WRITE,
    TICKET_CREATE,
    NOTIFICATION_SEND,
    HTTP_FETCH,
    RESEARCH_SEARCH,
}

PERMISSIONS = PermissionMap(
    {
        RoleName.VIEWER.value: _READ_ONLY,
        RoleName.OPERATOR.value: _OPERATE,
        RoleName.APPROVER.value: _OPERATE | {APPROVAL_DECIDE},
        RoleName.ADMIN.value: _OPERATE | {APPROVAL_DECIDE, USER_MANAGE, AUDIT_READ},
    }
)
