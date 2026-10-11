"""RetrievalAgent: find the most relevant passages and answer FROM them, with citations.

Pipeline: (1) search the knowledge base through the tool layer (so the search is audited and
shows up in the run timeline), (2) fence the passages as untrusted data, (3) ask the LLM for an
answer that cites passage ids, (4) verify the citations against what was actually retrieved.
Why step 4: a model can cite a source that was never retrieved. We keep only citations that map
to real passages, and mark the answer ``grounded=False`` when none survive.
Why passage ids (P1, P2, ...) instead of titles: ids survive being copied by a model; titles
get mangled. The ids are translated back to "Title #n" for the user.
Ingestion (upload -> chunk -> store) lives in ``services/documents.py``; this agent only reads.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.base import AgentContext, LLMAgent, tagged
from app.core.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.models.enums import AgentName
from app.tools.base import ToolContext

SEARCH_TOOL = "search_knowledge_base"
NO_RESULTS_ANSWER = "No relevant information was found in the knowledge base."


class RetrievalInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=10)
    attempt: int = Field(default=1, ge=1)
    plan_tools: frozenset[str] = frozenset({SEARCH_TOOL})
    approved: bool = False


class Citation(BaseModel):
    id: str
    citation: str
    title: str
    document_id: uuid.UUID
    chunk_index: int
    score: float
    snippet: str


class RetrievalOutput(BaseModel):
    query: str
    answer: str
    citations: list[Citation]
    passages_found: int
    grounded: bool


class _LLMAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)
    citations: list[str] = Field(default_factory=list, max_length=20)


class RetrievalAgent(LLMAgent[RetrievalInput, RetrievalOutput]):
    name = AgentName.RETRIEVAL
    allowed_tools = frozenset({SEARCH_TOOL})
    system_prompt = (
        "You are the Retrieval agent. Answer the question using ONLY the passages provided. "
        "Cite the passages you used by their ids (like P1). If the passages do not contain the "
        'answer, say so. Reply with ONLY a JSON object: {"answer": string, "citations": [ids]}. '
        + UNTRUSTED_NOTICE
    )

    async def run(self, payload: RetrievalInput, ctx: AgentContext) -> RetrievalOutput:
        result = await self.deps.executor.execute(
            SEARCH_TOOL,
            {"query": payload.query, "top_k": payload.top_k},
            ToolContext(
                principal=ctx.principal,
                session_factory=self.deps.session_factory,
                settings=self.deps.settings,
                run_id=ctx.run_id,
                step_id=ctx.step_id,
                side_effect_key=str(ctx.step_id),
                approved=payload.approved,
            ),
            attempt=payload.attempt,
            plan_tools=payload.plan_tools,
            agent_allowlist=self.allowed_tools,
        )
        passages: list[dict[str, Any]] = result.output.get("results", [])
        if not passages:
            return RetrievalOutput(
                query=payload.query,
                answer=NO_RESULTS_ANSWER,
                citations=[],
                passages_found=0,
                grounded=False,
            )
        by_id = {f"P{i}": _to_citation(f"P{i}", p) for i, p in enumerate(passages, start=1)}
        answer = await self._synthesise(payload.query, passages, ctx)
        cited_ids = [c for c in dict.fromkeys(answer.citations) if c in by_id]
        return RetrievalOutput(
            query=payload.query,
            answer=_humanise(answer.answer, by_id),
            citations=[by_id[c] for c in cited_ids],
            passages_found=len(passages),
            grounded=bool(cited_ids),
        )

    async def _synthesise(
        self, query: str, passages: list[dict[str, Any]], ctx: AgentContext
    ) -> _LLMAnswer:
        blocks = [
            wrap_untrusted(f"Title: {p['title']}\n\n{p['content']}", f"P{i}")
            for i, p in enumerate(passages, start=1)
        ]
        message = f"{tagged('question', query)}\n\n" + "\n\n".join(blocks)
        return await self.complete_json(
            task="retrieve_answer", user_message=message, model=_LLMAnswer, ctx=ctx
        )


def _to_citation(passage_id: str, passage: dict[str, Any]) -> Citation:
    return Citation(
        id=passage_id,
        citation=str(passage["citation"]),
        title=str(passage["title"]),
        document_id=uuid.UUID(str(passage["document_id"])),
        chunk_index=int(passage["chunk_index"]),
        score=float(passage["score"]),
        snippet=str(passage["content"])[:300],
    )


_MARKER = re.compile(r"\[?\b(P\d{1,2})\b\]?")


def _humanise(answer: str, by_id: dict[str, Citation]) -> str:
    """Replace ``[P1]`` markers with readable ``[Title #n]``; drop markers that are not real."""

    def replace(match: re.Match[str]) -> str:
        found = by_id.get(match.group(1))
        return f"[{found.citation}]" if found else ""

    return _MARKER.sub(replace, answer).strip()
