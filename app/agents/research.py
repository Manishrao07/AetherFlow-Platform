"""ResearchAgent: answer ONE sub-question from several sources at once, with citations.

Pipeline: (1) query every source tool the plan lists - web, Wikipedia, arXiv, the knowledge
base - IN PARALLEL through the tool layer (so each call is audited and appears in the run
timeline), (2) number the passages S1, S2, ..., (3) fence them as untrusted data and ask the LLM
for findings that cite those ids, (4) keep only citations that map to a real passage.
Why parallel and tolerant: sources fail independently (no API key, rate limit, timeout). One
broken source must not sink the sub-question, so a failure is recorded in ``sources_failed``;
only when EVERY source fails does the step fail (and then it is retried if any failure was
transient).
Why the same cite-the-id design as the RetrievalAgent: ids survive being copied by a model.
Connects to: planner (creates the steps), synthesizer (merges the outputs), engine (runs it).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.base import AgentContext, LLMAgent, tagged
from app.agents.retrieval import SEARCH_TOOL
from app.core.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.models.enums import AgentName
from app.tools.base import ToolContext, ToolError

WEB_TOOL = "web_search"
WIKIPEDIA_TOOL = "wikipedia_search"
ARXIV_TOOL = "arxiv_search"
# Order = order in the prompt: curated sources first, open web last.
SOURCE_TOOLS = (SEARCH_TOOL, WIKIPEDIA_TOOL, ARXIV_TOOL, WEB_TOOL)
NO_FINDINGS = "No source returned relevant information for this question."
_SNIPPET_CHARS = 300


class ResearchStepInputs(BaseModel):
    """What the Planner puts in a research step's ``inputs`` (validated at planning time)."""

    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=3, max_length=300)
    sources: list[str] = Field(min_length=1, max_length=len(SOURCE_TOOLS))


class ResearchInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    question: str = Field(min_length=3, max_length=300)
    sources: list[str] = Field(min_length=1)
    max_results: int = Field(default=3, ge=1, le=10)
    attempt: int = Field(default=1, ge=1)
    plan_tools: frozenset[str]
    approved: bool = False


class Source(BaseModel):
    id: str  # S1, S2, ... local to this sub-question
    tool: str
    citation: str
    url: str | None = None
    snippet: str


class ResearchOutput(BaseModel):
    question: str
    findings: str
    sources: list[Source]
    sources_failed: list[str]
    grounded: bool


class _LLMFindings(BaseModel):
    findings: str = Field(min_length=1, max_length=3000)
    citations: list[str] = Field(default_factory=list, max_length=30)


def validate_step_inputs(inputs: dict[str, Any]) -> ResearchStepInputs:
    """Check a research step's inputs; raises ``ValueError`` (used by the Planner's validator)."""
    try:
        parsed = ResearchStepInputs.model_validate(inputs)
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        raise ValueError(f"invalid research inputs ({fields})") from exc
    unknown = sorted(set(parsed.sources) - set(ResearchAgent.allowed_tools))
    if unknown:
        raise ValueError(f"research sources not allowed: {', '.join(unknown)}")
    return parsed


class ResearchAgent(LLMAgent[ResearchInput, ResearchOutput]):
    name = AgentName.RESEARCH
    allowed_tools = frozenset(SOURCE_TOOLS)
    system_prompt = (
        "You are the Research agent. Answer the question using ONLY the passages provided. "
        "Write 2-5 factual sentences and cite the passages you used by their ids (like S1). "
        "If the passages do not answer the question, say so. Reply with ONLY a JSON object: "
        '{"findings": string, "citations": [ids]}. ' + UNTRUSTED_NOTICE
    )

    async def run(self, payload: ResearchInput, ctx: AgentContext) -> ResearchOutput:
        sources_to_query = [t for t in SOURCE_TOOLS if t in payload.sources]
        outcomes = await asyncio.gather(
            *(self._query(tool, payload, ctx) for tool in sources_to_query)
        )
        failed = [f"{tool}: {error}" for tool, _, error in outcomes if error is not None]
        passages: list[tuple[str, dict[str, Any]]] = [
            (tool, p) for tool, found, _ in outcomes for p in found
        ]
        if not passages:
            self._raise_if_all_failed(outcomes, len(sources_to_query))
            return ResearchOutput(
                question=payload.question,
                findings=NO_FINDINGS,
                sources=[],
                sources_failed=failed,
                grounded=False,
            )
        by_id = {f"S{i}": _to_source(f"S{i}", t, p) for i, (t, p) in enumerate(passages, start=1)}
        answer = await self._synthesise(payload.question, passages, ctx)
        cited = [c for c in dict.fromkeys(answer.citations) if c in by_id]
        return ResearchOutput(
            question=payload.question,
            findings=_drop_unknown_markers(answer.findings, set(by_id)),
            sources=list(by_id.values()),
            sources_failed=failed,
            grounded=bool(cited),
        )

    async def _query(
        self, tool: str, payload: ResearchInput, ctx: AgentContext
    ) -> tuple[str, list[dict[str, Any]], str | None]:
        """Run one source; return ``(tool, passages, error)`` - never raises for tool errors."""
        inputs: dict[str, Any] = (
            {"query": payload.question, "top_k": payload.max_results}
            if tool == SEARCH_TOOL
            else {"query": payload.question, "max_results": payload.max_results}
        )
        try:
            result = await self.deps.executor.execute(
                tool,
                inputs,
                ToolContext(
                    principal=ctx.principal,
                    session_factory=self.deps.session_factory,
                    settings=self.deps.settings,
                    run_id=ctx.run_id,
                    step_id=ctx.step_id,
                    # WHY tool in the key: one step calls several tools; each needs its own key.
                    side_effect_key=f"{ctx.step_id}:{tool}",
                    approved=payload.approved,
                ),
                attempt=payload.attempt,
                plan_tools=payload.plan_tools,
                agent_allowlist=self.allowed_tools,
            )
        except ToolError as exc:
            retry = "transient" if exc.retryable else "permanent"
            return tool, [], f"{retry}: {str(exc)[:160]}"
        return tool, list(result.output.get("results", [])), None

    @staticmethod
    def _raise_if_all_failed(
        outcomes: list[tuple[str, list[dict[str, Any]], str | None]], queried: int
    ) -> None:
        errors = [e for _, _, e in outcomes if e is not None]
        if len(errors) < queried:
            return  # some source answered with "nothing found": a valid, empty result
        # WHY retry only if something was transient: a missing API key will never fix itself.
        retryable = any(e.startswith("transient") for e in errors)
        raise ToolError(
            "every research source failed: " + "; ".join(errors)[:400], retryable=retryable
        )

    async def _synthesise(
        self, question: str, passages: list[tuple[str, dict[str, Any]]], ctx: AgentContext
    ) -> _LLMFindings:
        blocks = [
            wrap_untrusted(f"Source: {p['citation']}\n\n{p['content']}", f"S{i}")
            for i, (_, p) in enumerate(passages, start=1)
        ]
        message = f"{tagged('question', question)}\n\n" + "\n\n".join(blocks)
        return await self.complete_json(
            task="research_findings",
            user_message=message,
            model=_LLMFindings,
            ctx=ctx,
            max_tokens=700,
        )


def _to_source(source_id: str, tool: str, passage: dict[str, Any]) -> Source:
    url = passage.get("url")
    return Source(
        id=source_id,
        tool=tool,
        citation=str(passage["citation"])[:200],
        url=str(url)[:500] if url else None,
        snippet=" ".join(str(passage["content"]).split())[:_SNIPPET_CHARS],
    )


_MARKER = re.compile(r"\[(S\d{1,3})\]")


def _drop_unknown_markers(text: str, known: set[str]) -> str:
    """Remove ``[S9]``-style markers the model invented (no such passage was retrieved)."""
    return _MARKER.sub(lambda m: m.group(0) if m.group(1) in known else "", text).strip()
