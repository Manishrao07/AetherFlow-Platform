"""ReviewAgent: LLM verdicts plus the policy rules the model cannot override."""

import json
from typing import Any

from app.agents.base import AgentContext
from app.agents.plan import Plan
from app.agents.review import ReviewInput, ReviewResult, StepResult, enforce_policy
from app.models.enums import AgentName, ReviewVerdict, StepStatus
from tests.conftest import make_agents, make_principal

PLAN = Plan.model_validate(
    {
        "summary": "p",
        "steps": [
            {
                "key": "s1", "description": "search", "agent": "retrieval",
                "tool": "search_knowledge_base", "inputs": {"query": "q"},
            }
        ],
    }
)  # fmt: skip


def result(status: StepStatus = StepStatus.SUCCEEDED, key: str = "s1") -> StepResult:
    return StepResult(
        key=key, agent=AgentName.RETRIEVAL, tool="search_knowledge_base", status=status,
        output={"answer": "ok"} if status is StepStatus.SUCCEEDED else None,
        error=None if status is StepStatus.SUCCEEDED else "boom",
    )  # fmt: skip


def review_input(goal: str = "Find policy", *results: StepResult, **extra: Any) -> ReviewInput:
    return ReviewInput(goal=goal, plan=PLAN, results=list(results or [result()]), **extra)


CTX = AgentContext(principal=make_principal())


async def test_successful_run_is_approved() -> None:
    agents, provider = make_agents()
    verdict = await agents.review.run(review_input(), CTX)
    assert verdict.verdict is ReviewVerdict.APPROVED and verdict.score >= 0.7
    assert provider.calls[0].task == "review"
    assert "<untrusted_data" in provider.calls[0].messages[0].content


async def test_failed_step_needs_revision_with_feedback() -> None:
    agents, _ = make_agents()
    verdict = await agents.review.run(review_input("Find policy", result(StepStatus.FAILED)), CTX)
    assert verdict.verdict is ReviewVerdict.NEEDS_REVISION
    assert verdict.feedback


async def test_revision_hook_asks_once_then_approves() -> None:
    agents, _ = make_agents()
    first = await agents.review.run(review_input("Summarise [force-revision]"), CTX)
    assert first.verdict is ReviewVerdict.NEEDS_REVISION and first.feedback
    second = await agents.review.run(
        review_input("Summarise [force-revision]", revision=1, previous_feedback=first.feedback),
        CTX,
    )
    assert second.verdict is ReviewVerdict.APPROVED


async def test_reject_hook() -> None:
    agents, _ = make_agents()
    verdict = await agents.review.run(review_input("Do evil [force-reject]"), CTX)
    assert verdict.verdict is ReviewVerdict.REJECTED


async def test_model_cannot_approve_a_run_with_failed_steps() -> None:
    agents, provider = make_agents()
    provider.register(
        "review",
        lambda _r: json.dumps(
            {"verdict": "APPROVED", "score": 0.99, "reasons": ["looks great"], "feedback": ""}
        ),
    )
    verdict = await agents.review.run(review_input("g", result(StepStatus.FAILED)), CTX)
    assert verdict.verdict is ReviewVerdict.NEEDS_REVISION
    assert any("[policy]" in r for r in verdict.reasons)
    assert verdict.score < 0.7 and verdict.feedback


async def test_low_score_approval_is_downgraded() -> None:
    low = ReviewResult(verdict=ReviewVerdict.APPROVED, score=0.5, reasons=["meh"])
    out = enforce_policy(low, [result()], min_score=0.7)
    assert out.verdict is ReviewVerdict.NEEDS_REVISION
    assert "below the minimum" in out.reasons[-1] and out.score == 0.5


def test_rejection_and_good_approval_pass_through_unchanged() -> None:
    rejected = ReviewResult(verdict=ReviewVerdict.REJECTED, score=0.1, reasons=["unsafe"])
    assert enforce_policy(rejected, [result(StepStatus.FAILED)], 0.7) == rejected
    good = ReviewResult(verdict=ReviewVerdict.APPROVED, score=0.8, reasons=["fine"])
    assert enforce_policy(good, [result()], 0.7) == good


def test_skipped_step_blocks_approval() -> None:
    ok = ReviewResult(verdict=ReviewVerdict.APPROVED, score=0.95, reasons=["fine"])
    out = enforce_policy(ok, [result(), result(StepStatus.SKIPPED, "s2")], 0.7)
    assert out.verdict is ReviewVerdict.NEEDS_REVISION and "s2" in out.reasons[-1]


def test_empty_feedback_is_filled_from_reasons() -> None:
    bare = ReviewResult(verdict=ReviewVerdict.NEEDS_REVISION, score=0.3, reasons=["a", "b"])
    assert enforce_policy(bare, [result()], 0.7).feedback == "a; b"
