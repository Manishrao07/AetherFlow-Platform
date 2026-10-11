"""Confidence score, PDF rendering and the follow-up answerer."""

import json

import pytest

from app.agents.base import AgentContext
from app.agents.synthesizer import FollowUpInput, ReportSource, answer_followup, confidence_for
from app.services.reports import markdown_to_pdf
from tests.conftest import make_agents, make_principal

REPORT = (
    "# Research report: Refunds\n\n## Summary\n"
    "Digital goods are refundable only if the licence key has not been activated. [1]\n\n"
    "## Travel\nHotel stays are capped at 200 USD per night. [2]"
)
SOURCES = [
    ReportSource(n=1, tool="search_knowledge_base", citation="Refund Policy #1"),
    ReportSource(n=2, tool="search_knowledge_base", citation="Travel Policy #1", url="http://t"),
]


def test_confidence_blends_coverage_sources_and_review() -> None:
    out = {"report": "x", "coverage": 1.0, "sources": [{}] * 5}
    assert confidence_for([out], 1.0) == {
        "score": 1.0, "label": "high", "coverage": 1.0, "sources": 5, "review_score": 1.0
    }  # fmt: skip
    weak = confidence_for([{"report": "x", "coverage": 0.25, "sources": []}], 0.5)
    assert weak is not None and weak["label"] == "low" and weak["score"] == 0.28
    medium = confidence_for([{"report": "x", "coverage": 0.75, "sources": [{}] * 2}], 0.8)
    assert medium is not None and medium["label"] == "medium"


def test_no_confidence_for_runs_without_a_report() -> None:
    assert confidence_for([{"results": []}, {"answer": "a"}], 0.9) is None


def test_pdf_is_a_valid_document_and_escapes_markup() -> None:
    pdf = markdown_to_pdf(
        "# Title\n\n*Confidence: high*\n\n## Part\n- a <script>alert(1)</script> & b\n"
        "1. Source one - http://x\n\nPlain **bold** text."
    )
    assert pdf.startswith(b"%PDF") and len(pdf) > 1000


async def test_follow_up_is_answered_from_the_report_with_real_citations() -> None:
    agents, provider = make_agents()
    out = await answer_followup(
        agents.synthesizer,
        FollowUpInput(question="What is the hotel cap per night?", report=REPORT, sources=SOURCES),
        AgentContext(make_principal()),
    )
    assert "200 USD" in out.answer and [c.n for c in out.citations] == [2]
    assert provider.calls[0].task == "followup_answer"
    assert "<untrusted_data" in provider.calls[0].messages[0].content


async def test_follow_up_outside_the_report_says_so() -> None:
    agents, _ = make_agents()
    out = await answer_followup(
        agents.synthesizer,
        FollowUpInput(question="Who won the cricket world cup?", report=REPORT, sources=SOURCES),
        AgentContext(make_principal()),
    )
    assert out.answer == "The report does not cover that question." and out.citations == []


async def test_follow_up_drops_invented_citation_numbers() -> None:
    agents, provider = make_agents()
    provider.register(
        "followup_answer",
        lambda _r: json.dumps({"answer": "Yes [1] and [9].", "citations": [9, 1]}),
    )
    out = await answer_followup(
        agents.synthesizer,
        FollowUpInput(question="Is it refundable?", report=REPORT, sources=SOURCES),
        AgentContext(make_principal()),
    )
    assert out.answer == "Yes [1] and ." and [c.n for c in out.citations] == [1]


@pytest.mark.parametrize("question", ["hi", "x" * 501])
def test_follow_up_question_length_is_validated(question: str) -> None:
    with pytest.raises(ValueError, match="question"):
        FollowUpInput(question=question, report="r")
