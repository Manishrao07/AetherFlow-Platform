"""Schemas for the knowledge base."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DocumentStatus


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    # WHY max_length here too: rejected before any chunking work; mirrors DOCUMENT_MAX_CHARS
    # (the service re-checks against the configured value).
    content: str = Field(min_length=1, max_length=1_000_000)
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "title": "Travel policy",
                "content": "Employees may book economy class for flights under 6 hours.",
            }
        }
    )


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    source: str
    owner_id: uuid.UUID
    status: DocumentStatus
    chunk_count: int
    created_at: datetime


class IngestOut(BaseModel):
    document: DocumentOut
    created: bool = Field(description="False when identical content was already ingested")


class PassageOut(BaseModel):
    document_id: uuid.UUID
    title: str
    chunk_index: int
    citation: str
    score: float
    content: str
