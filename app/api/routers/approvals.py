"""Approval inbox: list approvals and approve / reject them."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import ApprovalServiceDep, ClientIpDep, require_permission_dep
from app.api.schemas.common import Page, PageParams, page_params
from app.api.schemas.workflows import ApprovalOut, ApproveBody, RejectBody
from app.core.rbac import Principal
from app.models.enums import ApprovalStatus
from app.orchestrator.approvals import ApprovalView
from app.services.permissions import APPROVAL_DECIDE, APPROVAL_READ

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

ReaderDep = Annotated[Principal, Depends(require_permission_dep(APPROVAL_READ))]
DeciderDep = Annotated[Principal, Depends(require_permission_dep(APPROVAL_DECIDE))]
PageDep = Annotated[PageParams, Depends(page_params)]


def approval_out(view: ApprovalView, *, include_plan: bool = True) -> ApprovalOut:
    a = view.approval
    return ApprovalOut(
        id=a.id, run_id=a.run_id, kind=a.kind, status=a.status, risk_level=a.risk_level,
        reason=a.reason, requested_by=a.requested_by, requester_email=view.requester_email,
        decided_by=a.decided_by, comment=a.comment, expires_at=a.expires_at,
        decided_at=a.decided_at, created_at=a.created_at, goal=view.goal,
        plan=view.plan if include_plan else None,
    )  # fmt: skip


@router.get("", response_model=Page[ApprovalOut])
async def list_approvals(
    user: ReaderDep,
    service: ApprovalServiceDep,
    page: PageDep,
    status: Annotated[ApprovalStatus | None, Query()] = ApprovalStatus.PENDING,
) -> Page[ApprovalOut]:
    """Pending approvals by default (approvers/admins: everyone's; others: only your own)."""
    views, total = await service.list_for(user, status, page)
    return Page(
        items=[approval_out(v) for v in views], total=total, limit=page.limit, offset=page.offset
    )


@router.get("/{approval_id}", response_model=ApprovalOut)
async def get_approval(
    approval_id: uuid.UUID, user: ReaderDep, service: ApprovalServiceDep
) -> ApprovalOut:
    return approval_out(await service.get(user, approval_id))


@router.post("/{approval_id}/approve", response_model=ApprovalOut)
async def approve(
    approval_id: uuid.UUID,
    body: ApproveBody,
    user: DeciderDep,
    service: ApprovalServiceDep,
    ip: ClientIpDep,
) -> ApprovalOut:
    """Approve the plan; the run resumes. You cannot approve your own request."""
    await service.decide(user, approval_id, approve=True, comment=body.comment, ip=ip)
    return approval_out(await service.get(user, approval_id))


@router.post("/{approval_id}/reject", response_model=ApprovalOut)
async def reject(
    approval_id: uuid.UUID,
    body: RejectBody,
    user: DeciderDep,
    service: ApprovalServiceDep,
    ip: ClientIpDep,
) -> ApprovalOut:
    """Reject the plan; the run is cancelled and the reason is recorded."""
    await service.decide(user, approval_id, approve=False, comment=body.comment, ip=ip)
    return approval_out(await service.get(user, approval_id))
