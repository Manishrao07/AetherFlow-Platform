"""Schemas for long-term memory."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import MemoryKind


class MemoryUpsert(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    kind: MemoryKind = MemoryKind.FACT
    importance: int = Field(default=1, ge=1, le=5)
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "content": "Prefers weekly summaries",
                "kind": "PREFERENCE",
                "importance": 3,
            }
        }
    )


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    key: str
    content: str
    kind: MemoryKind
    importance: int
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None


class ErasedOut(BaseModel):
    deleted: int
