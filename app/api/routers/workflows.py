"""Workflow endpoints: create, list, get, cancel, retry-step, timeline, live events (SSE)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.deps import ClientIpDep, ReportServiceDep, WorkflowServiceDep, require_permission_dep
from app.api.routers.approvals import approval_out
from app.api.schemas.common import Page, PageParams, page_params
from app.api.schemas.workflows import (
    AskRequest,
    AskResponse,
    EventOut,
    LLMCallOut,
    ReportSourceOut,
    SimilarRunOut,
    StepOut,
    TimelineOut,
    ToolExecutionOut,
    WorkflowCreate,
    WorkflowOut,
)
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.rbac import Principal
from app.models.enums import RunStatus
from app.orchestrator.state_machine import TERMINAL_RUN_STATUSES
from app.services.auth_service import AuthService
from app.services.permissions import (
    PERMISSIONS,
    WORKFLOW_CANCEL,
    WORKFLOW_CREATE,
    WORKFLOW_READ,
)
from app.services.workflows import WorkflowService, timeline_totals

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])

ReaderDep = Annotated[Principal, Depends(require_permission_dep(WORKFLOW_READ))]
CreatorDep = Annotated[Principal, Depends(require_permission_dep(WORKFLOW_CREATE))]
CancellerDep = Annotated[Principal, Depends(require_permission_dep(WORKFLOW_CANCEL))]
PageDep = Annotated[PageParams, Depends(page_params)]

_HEARTBEAT_SECONDS = 15.0


@router.post("", response_model=WorkflowOut, status_code=202)
async def create_workflow(
    body: WorkflowCreate,
    response: Response,
    user: CreatorDep,
    service: WorkflowServiceDep,
    ip: ClientIpDep,
    idempotency_key: Annotated[
        str | None,
        Header(
            max_length=128,
            description="Repeat the same key to safely retry; the original run is returned.",
        ),
    ] = None,
) -> WorkflowOut:
    """Start a multi-agent run. Returns immediately (202); follow progress via ``/events``."""
    run, created = await service.create(
        user, body.goal, conversation_id=body.conversation_id, max_revisions=body.max_revisions,
        idempotency_key=idempotency_key, ip=ip, mode=body.mode,
    )  # fmt: skip
    if not created:
        response.status_code = 200
    return WorkflowOut.model_validate(run)


@router.get("", response_model=Page[WorkflowOut])
async def list_workflows(
    user: ReaderDep,
    service: WorkflowServiceDep,
    page: PageDep,
    status: Annotated[RunStatus | None, Query()] = None,
    everyone: Annotated[
        bool, Query(description="Admins/approvers: include other users' runs")
    ] = False,
) -> Page[WorkflowOut]:
    rows, total = await service.list_runs(user, page, status, everyone)
    return Page(
        items=[WorkflowOut.model_validate(r) for r in rows],
        total=total, limit=page.limit, offset=page.offset,
    )  # fmt: skip


@router.get("/similar", response_model=list[SimilarRunOut])
async def similar_runs(
    user: ReaderDep,
    reports: ReportServiceDep,
    goal: Annotated[str, Query(min_length=3, max_length=4000)],
) -> list[SimilarRunOut]:
    """Your earlier completed runs on a similar topic ("did you already research this?")."""
    return [
        SimilarRunOut(
            id=s.run.id, goal=s.run.goal, mode=s.run.mode,
            similarity=round(s.similarity, 3), finished_at=s.run.finished_at,
        )
        for s in await reports.similar(user, goal)
    ]  # fmt: skip


@router.get("/{run_id}", response_model=WorkflowOut)
async def get_workflow(
    run_id: uuid.UUID, user: ReaderDep, service: WorkflowServiceDep
) -> WorkflowOut:
    return WorkflowOut.model_validate(await service.get(user, run_id))


@router.get("/{run_id}/report")
async def download_report(
    run_id: uuid.UUID,
    user: ReaderDep,
    reports: ReportServiceDep,
    format: Annotated[Literal["md", "pdf"], Query()] = "md",
) -> Response:
    """The finished report as Markdown or PDF (409 until the run has completed)."""
    if format == "pdf":
        data = await reports.report_pdf(user, run_id)
        return Response(
            data, media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="report-{str(run_id)[:8]}.pdf"'},
        )  # fmt: skip
    text = await reports.report_markdown(user, run_id)
    return Response(text, media_type="text/markdown; charset=utf-8")


@router.post("/{run_id}/ask", response_model=AskResponse)
async def ask_follow_up(
    run_id: uuid.UUID, body: AskRequest, user: CreatorDep, reports: ReportServiceDep
) -> AskResponse:
    """Ask a follow-up question; answered from the finished report only (no new research)."""
    answer = await reports.ask(user, run_id, body.question)
    return AskResponse(
        answer=answer.answer,
        citations=[ReportSourceOut(**c.model_dump()) for c in answer.citations],
    )


@router.post("/{run_id}/cancel", response_model=WorkflowOut)
async def cancel_workflow(
    run_id: uuid.UUID, user: CancellerDep, service: WorkflowServiceDep, ip: ClientIpDep
) -> WorkflowOut:
    """Stop a run (owner or admin). In-flight tool calls finish; nothing new starts."""
    return WorkflowOut.model_validate(await service.cancel(user, run_id, ip))


@router.post("/{run_id}/steps/{step_id}/retry", response_model=WorkflowOut, status_code=202)
async def retry_step(
    run_id: uuid.UUID,
    step_id: uuid.UUID,
    user: CreatorDep,
    service: WorkflowServiceDep,
    ip: ClientIpDep,
) -> WorkflowOut:
    """Re-run a failed step of a failed run (and the steps that were skipped because of it)."""
    return WorkflowOut.model_validate(await service.retry_step(user, run_id, step_id, ip))


@router.get("/{run_id}/timeline", response_model=TimelineOut)
async def get_timeline(
    run_id: uuid.UUID, user: ReaderDep, service: WorkflowServiceDep
) -> TimelineOut:
    """Everything that happened: steps, tool calls (redacted), LLM calls with cost, approvals."""
    t = await service.timeline(user, run_id)
    return TimelineOut(
        run=WorkflowOut.model_validate(t.run),
        steps=[StepOut.model_validate(s) for s in t.steps],
        tool_executions=[ToolExecutionOut.model_validate(x) for x in t.tool_executions],
        llm_calls=[LLMCallOut.model_validate(c) for c in t.llm_calls],
        approvals=[approval_out(v, include_plan=False) for v in t.approvals],
        events=[EventOut.model_validate(e) for e in t.events],
        totals=timeline_totals(t),
    )


# --------------------------------------------------------------------- SSE
async def _stream_principal(request: Request) -> Principal:
    """Authenticate WITHOUT holding a database session for the life of the stream."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError()
    state = request.app.state
    async with state.session_factory() as session:
        auth = AuthService(session, state.token_codec, state.settings, state.audit)
        principal = await auth.principal_from_access_token(token)
    if not PERMISSIONS.allows(principal.roles, WORKFLOW_READ):
        raise PermissionDeniedError("You do not have permission to perform this action")
    return principal


async def _event_stream(
    request: Request, run_id: uuid.UUID, principal: Principal, last_id: int
) -> AsyncIterator[str]:
    """Poll the event log and emit Server-Sent Events until the run reaches a terminal state."""
    state = request.app.state
    poll = state.settings.sse_poll_interval_seconds
    last_beat = time.monotonic()
    while True:
        async with state.session_factory() as session:
            service = WorkflowService(
                session, state.session_factory, state.run_store, state.audit,
                state.queue.enqueue, 0, 0,
            )  # fmt: skip
            events = await service.events_after(run_id, last_id)
            run = await service.get(principal, run_id)
        for event in events:
            last_id = event.id
            payload = json.dumps(event.data, default=str)
            yield f"id: {event.id}\nevent: {event.type}\ndata: {payload}\n\n"
            last_beat = time.monotonic()
        if not events and run.status in TERMINAL_RUN_STATUSES:
            yield f"event: end\ndata: {json.dumps({'status': run.status.value})}\n\n"
            return
        if time.monotonic() - last_beat > _HEARTBEAT_SECONDS:
            yield ": keep-alive\n\n"  # comment line: keeps proxies from closing an idle stream
            last_beat = time.monotonic()
        if await request.is_disconnected():
            return
        await asyncio.sleep(poll)


@router.get("/{run_id}/events", response_class=StreamingResponse)
async def stream_events(
    run_id: uuid.UUID,
    request: Request,
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Live progress as Server-Sent Events. Send ``Authorization: Bearer ...``.

    Every event carries an ``id``; a reconnecting client sends ``Last-Event-ID`` and receives
    only what it missed. The stream ends with an ``end`` event once the run is COMPLETED, FAILED
    or CANCELLED.
    """
    principal = await _stream_principal(request)
    state = request.app.state
    async with state.session_factory() as session:  # 404 now, not mid-stream
        await WorkflowService(
            session, state.session_factory, state.run_store, state.audit, state.queue.enqueue, 0, 0
        ).get(principal, run_id)
    return StreamingResponse(
        _event_stream(request, run_id, principal, last_event_id or 0),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
