"""Conversations and messages: create, list, history, append, archive.

What: ``ConversationService`` - every method takes the caller's ``user_id`` and only ever
touches that user's rows.
Why 404 (not 403) for someone else's conversation: a 403 would confirm the id exists.
Why messages get ``clock_timestamp()``: ``now()`` is frozen for the whole transaction, so two
messages inserted together would tie; ``clock_timestamp()`` keeps strict insertion order,
which the history and the short-term window rely on.
Connects to: conversations router, orchestrator (writes the assistant reply of a run).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.common import PageParams
from app.core.errors import ConflictError, NotFoundError
from app.models import Conversation, Message
from app.models.enums import ConversationStatus, MessageRole
from app.services.chunking import estimate_tokens


class ConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, user_id: uuid.UUID, title: str) -> Conversation:
        conversation = Conversation(user_id=user_id, title=title.strip())
        self._session.add(conversation)
        await self._session.flush()
        await self._session.refresh(conversation)
        return conversation

    async def list_for_user(
        self, user_id: uuid.UUID, page: PageParams, status: ConversationStatus | None
    ) -> tuple[list[Conversation], int]:
        conditions = [Conversation.user_id == user_id]
        if status is not None:
            conditions.append(Conversation.status == status)
        total = (
            await self._session.scalar(
                sa.select(sa.func.count()).select_from(Conversation).where(*conditions)
            )
            or 0
        )
        rows = await self._session.scalars(
            sa.select(Conversation)
            .where(*conditions)
            .order_by(Conversation.updated_at.desc(), Conversation.id)
            .limit(page.limit)
            .offset(page.offset)
        )
        return list(rows), total

    async def get(self, user_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation:
        conversation = await self._session.scalar(
            sa.select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user_id
            )
        )
        if conversation is None:
            raise NotFoundError("Conversation not found")
        return conversation

    async def history(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID, page: PageParams
    ) -> tuple[list[Message], int]:
        """Messages oldest-first (a chat is read top to bottom)."""
        await self.get(user_id, conversation_id)
        total = (
            await self._session.scalar(
                sa.select(sa.func.count())
                .select_from(Message)
                .where(Message.conversation_id == conversation_id)
            )
            or 0
        )
        rows = await self._session.scalars(
            sa.select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.id)
            .limit(page.limit)
            .offset(page.offset)
        )
        return list(rows), total

    async def append(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        content: str,
        role: MessageRole = MessageRole.USER,
        run_id: uuid.UUID | None = None,
    ) -> Message:
        conversation = await self.get(user_id, conversation_id)
        if conversation.status is ConversationStatus.ARCHIVED:
            raise ConflictError("Conversation is archived; unarchive it or start a new one")
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            token_count=estimate_tokens(content),
            run_id=run_id,
            created_at=sa.func.clock_timestamp(),
        )
        self._session.add(message)
        # WHY touch updated_at: the conversation list is ordered by recent activity.
        await self._session.execute(
            sa.update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(updated_at=sa.func.now())
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()
        await self._session.refresh(message)
        return message

    async def archive(self, user_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation:
        conversation = await self.get(user_id, conversation_id)
        conversation.status = ConversationStatus.ARCHIVED
        await self._session.flush()
        await self._session.refresh(conversation)
        return conversation
