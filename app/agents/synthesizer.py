"""SynthesizerAgent: merge the sub-question findings into ONE cited report.

Input comes from the research steps this step depends on (the engine passes their outputs as
``upstream``). The code, not the model, owns the citation numbering:
(1) every source of every sub-question gets a global number [1], [2], ... in a fixed order,
(2) the model sees findings that already use those numbers,
(3) after the model answers, markers that point at no real source are removed and a "Sources"
list is appended from the sources actually cited.
Why: a model that renumbers or invents references produces a report whose citations lie. This
way a reader can always follow [n] to a real URL or document.
It has no tools (least privilege): synthesis is pure writing, so it can never cause a side
effect, and its input is fenced as untrusted data because web text flows through it.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.base import AgentContext, LLMAgent, tagged
from app.agents.research import ResearchOutput
from app.core.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.models.enums import AgentName

MAX_REPORT_CHARS = 12000
NO_EVIDENCE_REPORT = (
    "No reliable sources were found for any part of this topic, so no report could be written. "
    "Try rephrasing the topic, uploading relevant documents, or configuring a web search key."
)


class SynthesizerStepInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=3, max_length=4000)


class SynthesizerInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    topic: str = Field(min_length=3, max_length=4000)
    # Outputs of the research steps, keyed by step key (the engine passes them as-is).
    upstream: dict[str, dict[str, Any]]


class ReportSource(BaseModel):
    n: int
    tool: str
    citation: str
    url: str | None = None


class SynthesizerOutput(BaseModel):
    topic: str
    report: str
    sources: list[ReportSource]
    questions_total: int
    questions_grounded: int
    coverage: float = Field(ge=0, le=1)


class _LLMReport(BaseModel):
    report: str = Field(min_length=1, max_length=MAX_REPORT_CHARS)
    citations: list[int] = Field(default_factory=list, max_length=100)


class SynthesizerAgent(LLMAgent[SynthesizerInput, SynthesizerOutput]):
    name = AgentName.SYNTHESIZER
    allowed_tools = frozenset()
    system_prompt = (
        "You are the Synthesizer agent. Write a clear research report in Markdown from the "
        "findings provided: a title line, a short summary, then one section per sub-question. "
        "Use ONLY the findings; never add facts. Keep the numeric citation markers like [1] next "
        "to the claims they support. If the topic compares options, include a Markdown comparison "
        "table and a verdict line. If a sub-question has no findings, say that it is open. "
        'Reply with ONLY a JSON object: {"report": string, "citations": [numbers]}. '
        + UNTRUSTED_NOTICE
    )

    async def run(self, payload: SynthesizerInput, ctx: AgentContext) -> SynthesizerOutput:
        outputs = [
            ResearchOutput.model_validate(o)
            for _, o in sorted(payload.upstream.items())
            if "findings" in o and "question" in o
        ]
        numbered, sources = _number_sources(outputs)
        grounded = sum(o.grounded for o in outputs)
        if not sources:
            return SynthesizerOutput(
                topic=payload.topic,
                report=NO_EVIDENCE_REPORT,
                sources=[],
                questions_total=len(outputs),
                questions_grounded=0,
                coverage=0.0,
            )
        answer = await self.complete_json(
            task="synthesize_report",
            user_message=_build_message(payload.topic, outputs, numbered, sources),
            model=_LLMReport,
            ctx=ctx,
            max_tokens=3000,
        )
        report, kept = finalise_report(answer, sources)
        return SynthesizerOutput(
            topic=payload.topic,
            report=report,
            sources=kept,
            questions_total=len(outputs),
            questions_grounded=grounded,
            coverage=round(grounded / len(outputs), 3) if outputs else 0.0,
        )


_LOCAL_MARKER = re.compile(r"\[(S\d{1,3})\]")
_NUM_MARKER = re.compile(r"\[(\d{1,3})\]")


def _number_sources(
    outputs: list[ResearchOutput],
) -> tuple[list[str], list[ReportSource]]:
    """Give every source a global number; rewrite each findings text to use them.

    Returns the rewritten findings (one string per research output) and the source table.
    Identical URLs/citations are numbered once, so the same page cited by two sub-questions
    is one reference.
    """
    sources: list[ReportSource] = []
    index: dict[str, int] = {}
    texts: list[str] = []
    for output in outputs:
        local: dict[str, int] = {}
        for src in output.sources:
            identity = src.url or src.citation
            if identity not in index:
                index[identity] = len(sources) + 1
                sources.append(
                    ReportSource(
                        n=index[identity], tool=src.tool, citation=src.citation, url=src.url
                    )
                )
            local[src.id] = index[identity]
        texts.append(_renumber(output.findings, local))
    return texts, sources


def _renumber(findings: str, local: dict[str, int]) -> str:
    """Replace local ``[S1]`` markers by global numbers; unknown markers are dropped."""

    def replace(match: re.Match[str]) -> str:
        number = local.get(match.group(1))
        return f"[{number}]" if number else ""

    return _LOCAL_MARKER.sub(replace, findings)


def _build_message(
    topic: str, outputs: list[ResearchOutput], texts: list[str], sources: list[ReportSource]
) -> str:
    findings = "\n\n".join(
        f"Sub-question {i}: {o.question}\nFindings: {text}"
        for i, (o, text) in enumerate(zip(outputs, texts, strict=True), start=1)
    )
    table = "\n".join(f"[{s.n}] {s.citation}" for s in sources)
    return "\n\n".join(
        [
            tagged("topic", topic),
            tagged("findings", wrap_untrusted(findings, "research findings")),
            tagged("sources", table),
        ]
    )


def finalise_report(
    answer: _LLMReport, sources: list[ReportSource]
) -> tuple[str, list[ReportSource]]:
    """Strip invented markers, renumber the cited sources 1..k, append the Sources list.

    WHY renumber: only cited sources are listed, so keeping the global numbers would show a
    list like "1, 3" that looks like a missing reference.
    """
    known = {s.n for s in sources}
    body = _NUM_MARKER.sub(
        lambda m: m.group(0) if int(m.group(1)) in known else "", answer.report
    ).strip()
    cited = {int(n) for n in _NUM_MARKER.findall(body)} & known
    cited |= {n for n in answer.citations if n in known}
    kept = [
        s.model_copy(update={"n": i})
        for i, s in enumerate((s for s in sources if s.n in cited), start=1)
    ]
    mapping = {old: new.n for old, new in zip(sorted(cited), kept, strict=True)}
    body = _NUM_MARKER.sub(
        lambda m: f"[{mapping[int(m.group(1))]}]" if int(m.group(1)) in mapping else m.group(0),
        body,
    )
    lines = [f"{s.n}. {s.citation}" + (f" - {s.url}" if s.url else "") for s in kept]
    if lines:
        body = f"{body}\n\n## Sources\n" + "\n".join(lines)
    return body[: MAX_REPORT_CHARS + 2000], kept


# ---------------------------------------------------------------- confidence
def confidence_for(outputs: list[dict[str, Any]], review_score: float) -> dict[str, Any] | None:
    """A 0..1 trust score for a finished report, from three things a reader can check.

    coverage (50%): share of sub-questions that found a grounded answer;
    sources (20%): how many distinct references the report cites (5+ = full marks);
    review (30%): the Review agent's own score.
    Returns ``None`` if the run produced no report (a standard, non-research run).
    """
    for output in outputs:
        if isinstance(output.get("report"), str) and "coverage" in output:
            sources = len(output.get("sources", []))
            score = (
                0.5 * float(output["coverage"]) + 0.2 * min(1.0, sources / 5) + 0.3 * review_score
            )
            score = round(min(1.0, max(0.0, score)), 2)
            label = "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"
            return {
                "score": score, "label": label, "coverage": output["coverage"],
                "sources": sources, "review_score": round(review_score, 2),
            }  # fmt: skip
    return None


# ----------------------------------------------------------------- follow-up
class FollowUpInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    question: str = Field(min_length=3, max_length=500)
    report: str = Field(min_length=1, max_length=20000)
    sources: list[ReportSource] = Field(default_factory=list)


class FollowUpAnswer(BaseModel):
    answer: str
    citations: list[ReportSource]


class _LLMFollowUp(BaseModel):
    answer: str = Field(min_length=1, max_length=3000)
    citations: list[int] = Field(default_factory=list, max_length=30)


FOLLOWUP_SYSTEM = (
    "You answer a follow-up question about a finished research report. Use ONLY the report "
    "text; never add outside facts. If the report does not cover the question, say so plainly. "
    "Keep the numeric citation markers like [1] next to the claims they support. "
    'Reply with ONLY a JSON object: {"answer": string, "citations": [numbers]}. ' + UNTRUSTED_NOTICE
)


async def answer_followup(
    agent: SynthesizerAgent, payload: FollowUpInput, ctx: AgentContext
) -> FollowUpAnswer:
    """Answer from the report alone - no new research, so it is fast, cheap and grounded."""
    message = f"{tagged('question', payload.question)}\n\n" + wrap_untrusted(
        payload.report, "research report"
    )
    result = await agent.complete_json(
        task="followup_answer",
        user_message=message,
        model=_LLMFollowUp,
        ctx=ctx,
        max_tokens=800,
        system=FOLLOWUP_SYSTEM,
    )
    known = {s.n: s for s in payload.sources}
    text = _NUM_MARKER.sub(
        lambda m: m.group(0) if int(m.group(1)) in known else "", result.answer
    ).strip()
    cited = sorted(
        ({int(n) for n in _NUM_MARKER.findall(text)} | set(result.citations)) & set(known)
    )
    return FollowUpAnswer(answer=text, citations=[known[n] for n in cited])
