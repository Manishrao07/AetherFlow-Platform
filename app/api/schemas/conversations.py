"""Schemas for conversations, messages and the short-term context window."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ConversationStatus, MessageRole


class ConversationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    model_config = ConfigDict(json_schema_extra={"example": {"title": "Refund policy questions"}})


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    status: ConversationStatus
    created_at: datetime
    updated_at: datetime


class MessageCreate(BaseModel):
    # WHY a cap: one message cannot flood the history, the summariser or the prompt budget.
    content: str = Field(min_length=1, max_length=8000)
    model_config = ConfigDict(
        json_schema_extra={"example": {"content": "What is our refund window for digital goods?"}}
    )


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    role: MessageRole
    content: str
    token_count: int
    run_id: uuid.UUID | None
    created_at: datetime


class ContextMessage(BaseModel):
    role: str
    content: str


class ContextOut(BaseModel):
    summary: str
    messages: list[ContextMessage]
    token_estimate: int
    newly_summarized: int
