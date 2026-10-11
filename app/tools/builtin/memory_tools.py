"""memory_write / memory_read: agents' access to the user's long-term memory.

Scope is fixed to the calling user's id from the trusted ToolContext - there is no input field
for a user id, so even a prompt-injected model cannot read or write another user's memory.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.memory.long_term import LongTermMemory
from app.models.enums import MemoryKind, RiskLevel
from app.services.permissions import MEMORY_READ, MEMORY_WRITE
from app.tools.base import Tool, ToolContext


class MemoryWriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=120, examples=["report_frequency"])
    content: str = Field(min_length=1, max_length=2000, examples=["Prefers weekly summaries"])
    kind: MemoryKind = MemoryKind.FACT
    importance: int = Field(default=1, ge=1, le=5)


class MemoryReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=200, description="Empty = most important memories")
    top_k: int = Field(default=5, ge=1, le=10)


class MemoryWriteTool(Tool):
    name = "memory_write"
    description = "Remember a fact or preference about the user (overwrites the same key)."
    input_model = MemoryWriteInput
    risk = RiskLevel.LOW
    permission = MEMORY_WRITE
    timeout_seconds = 5.0

    async def run(self, args: MemoryWriteInput, ctx: ToolContext) -> dict[str, Any]:
        async with ctx.session_factory() as session, session.begin():
            memory = await LongTermMemory(session).upsert(
                ctx.principal.user_id, args.key, args.content, args.kind, args.importance
            )
        return {"key": memory.key, "stored": True}


class MemoryReadTool(Tool):
    name = "memory_read"
    description = "Recall remembered facts/preferences about the user relevant to a query."
    input_model = MemoryReadInput
    risk = RiskLevel.LOW
    permission = MEMORY_READ
    timeout_seconds = 5.0

    async def run(self, args: MemoryReadInput, ctx: ToolContext) -> dict[str, Any]:
        async with ctx.session_factory() as session, session.begin():
            rows = await LongTermMemory(session).search(
                ctx.principal.user_id, args.query, args.top_k
            )
            memories = [
                {
                    "key": m.key,
                    "content": m.content,
                    "kind": m.kind.value,
                    "importance": m.importance,
                }
                for m in rows
            ]
        return {"memories": memories}
