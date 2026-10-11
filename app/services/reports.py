"""Reports: export a finished run's report, answer follow-ups, suggest earlier research.

What: ``ReportService`` reads a COMPLETED run's report (the synthesizer's Markdown) and offers
- ``report_markdown`` / ``report_pdf``: downloads,
- ``ask``: a follow-up question answered from the report alone (no new research),
- ``similar``: this user's earlier completed runs whose goal resembles a new one.
Why follow-ups use only the report: the answer stays grounded in sources the reader can already
see, costs one small LLM call, and cannot drift into unsourced claims.
Why ``similar`` only SUGGESTS: silently reusing an old answer could serve stale facts; the user
decides whether to open it or run fresh research.
Connects to: ``api/routers/workflows.py``; reads ``WorkflowRun.result`` written by the engine.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape

import sqlalchemy as sa
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentContext
from app.agents.synthesizer import (
    FollowUpAnswer,
    FollowUpInput,
    ReportSource,
    SynthesizerAgent,
    answer_followup,
)
from app.core.embeddings import Embedder, EmbeddingError, cosine
from app.core.errors import ConflictError
from app.core.rbac import Principal
from app.models import WorkflowRun
from app.models.enums import RunStatus
from app.services.workflows import WorkflowService

SIMILARITY_THRESHOLD = 0.5
_HISTORY_LIMIT = 50


@dataclass(frozen=True)
class Report:
    run: WorkflowRun
    markdown: str
    sources: list[ReportSource]
    confidence: dict[str, Any] | None


@dataclass(frozen=True)
class SimilarRun:
    run: WorkflowRun
    similarity: float


class ReportService:
    def __init__(
        self,
        session: AsyncSession,
        workflows: WorkflowService,
        synthesizer: SynthesizerAgent,
        embedder: Embedder | None,
    ) -> None:
        self._session = session
        self._workflows = workflows
        self._synthesizer = synthesizer
        self._embedder = embedder

    async def get(self, principal: Principal, run_id: Any) -> Report:
        """The report of a COMPLETED run the caller may see (409 otherwise)."""
        run = await self._workflows.get(principal, run_id)
        result = run.result or {}
        if run.status is not RunStatus.COMPLETED or not isinstance(result.get("summary"), str):
            raise ConflictError("This run has no finished report yet")
        sources: list[ReportSource] = []
        for output in (result.get("steps") or {}).values():
            if isinstance(output, dict) and isinstance(output.get("report"), str):
                sources = [ReportSource.model_validate(s) for s in output.get("sources", [])]
        return Report(run, result["summary"], sources, result.get("confidence"))

    async def report_markdown(self, principal: Principal, run_id: Any) -> str:
        report = await self.get(principal, run_id)
        return _with_confidence(report)

    async def report_pdf(self, principal: Principal, run_id: Any) -> bytes:
        return markdown_to_pdf(_with_confidence(await self.get(principal, run_id)))

    async def ask(self, principal: Principal, run_id: Any, question: str) -> FollowUpAnswer:
        report = await self.get(principal, run_id)
        return await answer_followup(
            self._synthesizer,
            FollowUpInput(question=question, report=report.markdown, sources=report.sources),
            AgentContext(principal, report.run.id),
        )

    async def similar(self, principal: Principal, goal: str, limit: int = 3) -> list[SimilarRun]:
        """Earlier completed runs of THIS user whose goal is close to ``goal`` (best first)."""
        if self._embedder is None:
            return []
        past = list(
            await self._session.scalars(
                sa.select(WorkflowRun)
                .where(
                    WorkflowRun.user_id == principal.user_id,
                    WorkflowRun.status == RunStatus.COMPLETED,
                )
                .order_by(WorkflowRun.created_at.desc())
                .limit(_HISTORY_LIMIT)
            )
        )
        if not past:
            return []
        try:
            vectors = await self._embedder.embed([goal, *(r.goal for r in past)])
        except EmbeddingError:
            return []  # a suggestion is optional: never fail the request for it
        scored = [
            SimilarRun(r, cosine(vectors[0], v)) for r, v in zip(past, vectors[1:], strict=True)
        ]
        best = [s for s in scored if s.similarity >= SIMILARITY_THRESHOLD]
        return sorted(best, key=lambda s: -s.similarity)[:limit]


def _with_confidence(report: Report) -> str:
    if not report.confidence:
        return report.markdown
    c = report.confidence
    line = (
        f"*Confidence: {c['label']} ({c['score']:.2f}) - "
        f"{c['sources']} sources, {int(float(c['coverage']) * 100)}% of questions answered*"
    )
    head, _, rest = report.markdown.partition("\n")
    return f"{head}\n\n{line}\n{rest}"


_BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.*)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"\*(.+?)\*")


def _inline(text: str) -> str:
    """Markdown bold/italic -> reportlab markup, after escaping everything else."""
    return _ITALIC.sub(r"<i>\1</i>", _BOLD.sub(r"<b>\1</b>", escape(text)))


def markdown_to_pdf(markdown: str) -> bytes:
    """Render the small Markdown subset our reports use (headings, bullets, paragraphs)."""
    base = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=base["BodyText"], fontSize=10.5, leading=15)
    h1 = ParagraphStyle("h1", parent=base["Title"], fontSize=20, leading=24, alignment=0)
    h2 = ParagraphStyle(
        "h2", parent=base["Heading2"], fontSize=13, textColor=colors.HexColor("#1f3a5f")
    )
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=14, bulletIndent=2)
    story: list[Any] = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            story.append(Spacer(1, 4))
        elif line.startswith("# "):
            story.append(Paragraph(_inline(line[2:]), h1))
        elif line.startswith("## ") or line.startswith("### "):
            story.append(Paragraph(_inline(line.lstrip("# ")), h2))
        elif match := _BULLET.match(line):
            story.append(Paragraph(_inline(match.group(1)), bullet, bulletText="•"))
        else:
            story.append(Paragraph(_inline(line), body))
    buffer = io.BytesIO()
    SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm,
        bottomMargin=2 * cm, title="Research report",
    ).build(story)  # fmt: skip
    return buffer.getvalue()
