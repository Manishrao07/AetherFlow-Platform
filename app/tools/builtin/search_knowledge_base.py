"""search_knowledge_base: top-k passages from ingested documents, with citations."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import RiskLevel
from app.services.knowledge import KnowledgeSearch
from app.services.permissions import DOCUMENT_READ
from app.tools.base import Tool, ToolContext


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500, examples=["refund policy for digital goods"])
    top_k: int = Field(default=5, ge=1, le=10)


class SearchKnowledgeBaseTool(Tool):
    name = "search_knowledge_base"
    description = "Search ingested company documents; returns ranked passages with citations."
    input_model = SearchInput
    risk = RiskLevel.LOW
    permission = DOCUMENT_READ
    timeout_seconds = 10.0

    def __init__(self, knowledge: KnowledgeSearch | None = None) -> None:
        self._knowledge = knowledge or KnowledgeSearch()

    async def run(self, args: SearchInput, ctx: ToolContext) -> dict[str, Any]:
        async with ctx.session_factory() as session:
            passages = await self._knowledge.search(session, args.query, args.top_k)
        return {
            "query": args.query,
            "results": [
                {
                    "document_id": str(p.document_id),
                    "title": p.title,
                    "chunk_index": p.chunk_index,
                    "citation": p.citation,
                    "score": round(p.score, 4),
                    "content": p.content,
                }
                for p in passages
            ],
        }
