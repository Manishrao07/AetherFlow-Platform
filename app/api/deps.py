"""FastAPI dependencies shared by routers.

What: database session per request, service factories, the authenticated ``Principal`` and
role/permission guards.
Why: dependency injection keeps routers thin and lets tests swap pieces. Authorization lives
HERE (declared per route) rather than inside handlers, so a route that forgets a guard is
visible at a glance in its signature.
Connects to: ``app/main.py`` (puts shared objects on ``app.state``) and every router.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditService
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.logging import bind_log_context
from app.core.rbac import Principal
from app.core.security_middleware import client_ip
from app.memory.long_term import LongTermMemory
from app.orchestrator.approvals import ApprovalService
from app.orchestrator.store import RunStore
from app.services.auth_service import AuthService
from app.services.conversations import ConversationService
from app.services.documents import DocumentService
from app.services.permissions import PERMISSIONS
from app.services.reports import ReportService
from app.services.user_service import UserService
from app.services.workflows import WorkflowService

# auto_error=False so a missing header goes through OUR error envelope, not FastAPI's default.
_bearer = HTTPBearer(auto_error=False, description="Access token from /api/v1/auth/login")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a session; commit if the request succeeded, roll back if it raised.

    WHY: one transaction per request means a failed request never leaves half-written data.
    """
    async with request.app.state.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_audit(request: Request) -> AuditService:
    audit: AuditService = request.app.state.audit
    return audit


AuditDep = Annotated[AuditService, Depends(get_audit)]


def get_auth_service(request: Request, session: SessionDep, audit: AuditDep) -> AuthService:
    state = request.app.state
    return AuthService(session, state.token_codec, state.settings, audit)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_user_service(session: SessionDep, audit: AuditDep) -> UserService:
    return UserService(session, audit)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]


def get_conversation_service(session: SessionDep) -> ConversationService:
    return ConversationService(session)


ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]


def get_document_service(request: Request, session: SessionDep, audit: AuditDep) -> DocumentService:
    return DocumentService(session, audit, request.app.state.knowledge)


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]


def get_long_term_memory(session: SessionDep) -> LongTermMemory:
    return LongTermMemory(session)


LongTermMemoryDep = Annotated[LongTermMemory, Depends(get_long_term_memory)]


def get_store(request: Request) -> RunStore:
    store: RunStore = request.app.state.run_store
    return store


def get_workflow_service(request: Request, session: SessionDep, audit: AuditDep) -> WorkflowService:
    state = request.app.state
    return WorkflowService(
        session,
        state.session_factory,
        state.run_store,
        audit,
        state.queue.enqueue,
        state.settings.default_max_revisions,
        state.settings.step_max_attempts,
    )


WorkflowServiceDep = Annotated[WorkflowService, Depends(get_workflow_service)]


def get_report_service(
    request: Request, session: SessionDep, workflows: WorkflowServiceDep
) -> ReportService:
    state = request.app.state
    return ReportService(session, workflows, state.agents.synthesizer, state.knowledge.embedder)


ReportServiceDep = Annotated[ReportService, Depends(get_report_service)]


def get_approval_service(request: Request, session: SessionDep, audit: AuditDep) -> ApprovalService:
    state = request.app.state
    return ApprovalService(
        session, state.session_factory, state.run_store, audit, state.queue.enqueue
    )


ApprovalServiceDep = Annotated[ApprovalService, Depends(get_approval_service)]


def get_client_ip(request: Request) -> str | None:
    return client_ip(request.scope, request.app.state.settings.trust_proxy_headers)


ClientIpDep = Annotated[str | None, Depends(get_client_ip)]


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    auth: AuthServiceDep,
) -> Principal:
    """Authenticate the request from its bearer token (roles are read fresh from the DB)."""
    if credentials is None:
        raise AuthenticationError()
    principal = await auth.principal_from_access_token(credentials.credentials)
    bind_log_context(user_id=str(principal.user_id))  # every later log line carries user_id
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def _deny(request: Request, principal: Principal, audit: AuditService, needed: str) -> None:
    """Audit a refused action, then raise 403."""
    await audit.record(
        "access.denied", actor_id=principal.user_id, actor_email=principal.email,
        outcome="denied", resource_type="endpoint",
        details={"method": request.method, "path": request.url.path, "needed": needed},
        ip_address=client_ip(request.scope, request.app.state.settings.trust_proxy_headers),
    )  # fmt: skip
    raise PermissionDeniedError(
        "You do not have permission to perform this action", details={"needed": needed}
    )


def require_roles_dep(*allowed: str) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: caller must hold at least one of ``allowed`` roles."""

    async def dependency(request: Request, principal: PrincipalDep, audit: AuditDep) -> Principal:
        if not principal.has_any_role(allowed):
            await _deny(request, principal, audit, "role:" + "|".join(sorted(allowed)))
        return principal

    return dependency


def require_permission_dep(permission: str) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: one of the caller's roles must grant ``permission``."""

    async def dependency(request: Request, principal: PrincipalDep, audit: AuditDep) -> Principal:
        if not PERMISSIONS.allows(principal.roles, permission):
            await _deny(request, principal, audit, permission)
        return principal

    return dependency
