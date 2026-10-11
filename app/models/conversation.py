"""Conversations and their messages.

What: ``Conversation`` (a chat thread owned by one user) and ``Message`` (one turn).
Why: a conversation can trigger many workflow runs, so runs point at the conversation, not
the other way round; ``messages.run_id`` links an assistant reply back to the run that
produced it.
Connects to: short-term memory (sliding window + summary stored on the conversation).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_column
from app.models.enums import ConversationStatus, MessageRole


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        enum_column(ConversationStatus, "conversation_status"),
        nullable=False,
        default=ConversationStatus.ACTIVE,
        server_default=ConversationStatus.ACTIVE.value,
    )
    # WHY here: the rolling summary of older messages belongs to the conversation (1:1).
    summary: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    summarized_until_message_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )

    # WHY: serves "my conversations, by status, most recently active first" (list endpoint).
    __table_args__ = (
        sa.Index("ix_conversations_user_status_updated", "user_id", "status", "updated_at"),
    )


class Message(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(
        enum_column(MessageRole, "message_role"), nullable=False
    )
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    token_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    # WHY SET NULL: deleting a run must not delete the chat history that mentions it.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.CheckConstraint("token_count >= 0", name="token_count_non_negative"),
        # WHY: history is always read as "this conversation, in time order".
        sa.Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        sa.Index("ix_messages_run_id", "run_id"),
    )
