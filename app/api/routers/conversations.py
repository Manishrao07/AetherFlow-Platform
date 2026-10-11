"""Conversation endpoints: create, list, history, append message, archive, context window."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import ConversationServiceDep, SessionDep, require_permission_dep
from app.api.schemas.common import Page, PageParams, page_params
from app.api.schemas.conversations import (
    ContextMessage,
    ContextOut,
    ConversationCreate,
    ConversationOut,
    MessageCreate,
    MessageOut,
)
from app.core.rbac import Principal
from app.memory.short_term import ShortTermMemory
from app.models.enums import ConversationStatus
from app.services.permissions import CONVERSATION_READ, CONVERSATION_WRITE

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

ReaderDep = Annotated[Principal, Depends(require_permission_dep(CONVERSATION_READ))]
WriterDep = Annotated[Principal, Depends(require_permission_dep(CONVERSATION_WRITE))]
PageDep = Annotated[PageParams, Depends(page_params)]


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(
    body: ConversationCreate, user: WriterDep, service: ConversationServiceDep
) -> ConversationOut:
    """Start a conversation (a thread that can trigger several workflow runs)."""
    return ConversationOut.model_validate(await service.create(user.user_id, body.title))


@router.get("", response_model=Page[ConversationOut])
async def list_conversations(
    user: ReaderDep,
    service: ConversationServiceDep,
    page: PageDep,
    status: Annotated[ConversationStatus | None, Query()] = None,
) -> Page[ConversationOut]:
    """Your conversations, most recently active first."""
    rows, total = await service.list_for_user(user.user_id, page, status)
    return Page(
        items=[ConversationOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: uuid.UUID, user: ReaderDep, service: ConversationServiceDep
) -> ConversationOut:
    return ConversationOut.model_validate(await service.get(user.user_id, conversation_id))


@router.get("/{conversation_id}/messages", response_model=Page[MessageOut])
async def get_history(
    conversation_id: uuid.UUID, user: ReaderDep, service: ConversationServiceDep, page: PageDep
) -> Page[MessageOut]:
    """Message history, oldest first."""
    rows, total = await service.history(user.user_id, conversation_id, page)
    return Page(
        items=[MessageOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post("/{conversation_id}/messages", response_model=MessageOut, status_code=201)
async def append_message(
    conversation_id: uuid.UUID,
    body: MessageCreate,
    user: WriterDep,
    service: ConversationServiceDep,
) -> MessageOut:
    """Add a user message. (Assistant replies are written by workflow runs, never by clients.)"""
    return MessageOut.model_validate(
        await service.append(user.user_id, conversation_id, body.content)
    )


@router.post("/{conversation_id}/archive", response_model=ConversationOut)
async def archive_conversation(
    conversation_id: uuid.UUID, user: WriterDep, service: ConversationServiceDep
) -> ConversationOut:
    """Archive a conversation; archived conversations accept no new messages."""
    return ConversationOut.model_validate(await service.archive(user.user_id, conversation_id))


@router.post("/{conversation_id}/context", response_model=ContextOut)
async def build_context(
    conversation_id: uuid.UUID,
    request: Request,
    user: WriterDep,
    service: ConversationServiceDep,
    session: SessionDep,
) -> ContextOut:
    """The history window agents would see (summary + recent messages, within the token budget).

    POST because it may persist a refreshed rolling summary; repeating it is harmless.
    """
    conversation = await service.get(user.user_id, conversation_id)
    settings = request.app.state.settings
    memory = ShortTermMemory(
        session,
        request.app.state.llm,
        token_budget=settings.short_term_token_budget,
        keep_recent=settings.short_term_keep_recent,
        summary_max_tokens=settings.summary_max_tokens,
    )
    window = await memory.build(conversation)
    return ContextOut(
        summary=window.summary,
        messages=[ContextMessage(role=m.role, content=m.content) for m in window.messages],
        token_estimate=window.token_estimate,
        newly_summarized=window.newly_summarized,
    )
