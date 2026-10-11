"""ReviewAgent: judges the run's results against the goal and the plan.

Verdicts: APPROVED | NEEDS_REVISION | REJECTED, with a 0..1 score and reasons.
Why code enforces policy on top of the model: an LLM can wave through broken work. After parsing
we apply rules the model cannot override: (a) APPROVED needs every step to have succeeded,
(b) APPROVED needs ``score >= REVIEW_MIN_SCORE``, (c) NEEDS_REVISION must carry feedback the
Planner can act on. Violations are downgraded and the reason is recorded as "[policy]".
Step outputs are fenced as untrusted data: a tool result (e.g. a fetched web page) must not be
able to talk the reviewer into approving.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.base import AgentContext, LLMAgent, tagged
from app.agents.plan import Plan
from app.core.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.models.enums import AgentName, ReviewVerdict, StepStatus

_OUTPUT_PREVIEW_CHARS = 1200
_REPORT_PREVIEW_CHARS = 4000


class StepResult(BaseModel):
    key: str
    agent: AgentName
    tool: str | None = None
    status: StepStatus
    output: dict[str, Any] | None = None
    error: str | None = None


class ReviewInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    goal: str
    plan: Plan
    results: list[StepResult]
    revision: int = Field(default=0, ge=0)
    previous_feedback: str | None = None


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: ReviewVerdict
    score: float = Field(ge=0, le=1)
    reasons: list[str] = Field(min_length=1, max_length=10)
    feedback: str = Field(default="", max_length=2000)


class ReviewAgent(LLMAgent[ReviewInput, ReviewResult]):
    name = AgentName.REVIEW
    allowed_tools = frozenset()  # WHY none: a reviewer that can act is no longer independent.
    system_prompt = (
        "You are the Review agent. Check whether the step results fully and safely achieve the "
        "goal. Look at completeness, correctness against the plan, and policy/safety problems "
        "(leaked secrets, unapproved side effects, ungrounded claims). "
        "Critical environment rules: "
        "(1) Security & Privacy: PII and secrets in tool outputs are automatically masked "
        "(e.g. emails appear as '[EMAIL]', tokens as '[REDACTED]'). "
        "(2) Mock Sandbox Tools: Tools in this environment return status 'sent (mock)' and "
        "created=True to avoid dispatching actual external spam emails. "
        "Treat 'sent (mock)' with recipient '[EMAIL]' as fully successful delivery to "
        "the requested recipient. Do NOT reject or request revision because of '[EMAIL]' "
        "or 'sent (mock)'! "
        "Reply with ONLY a JSON object: "
        '{"verdict": "APPROVED"|"NEEDS_REVISION"|"REJECTED", '
        '"score": number 0..1, "reasons": [string], "feedback": string}. '
        "Use NEEDS_REVISION when a better plan could fix the problem (feedback must say how) and "
        "REJECTED when the goal is unsafe or impossible. " + UNTRUSTED_NOTICE
    )

    async def run(self, payload: ReviewInput, ctx: AgentContext) -> ReviewResult:
        result = await self.complete_json(
            task="review",
            user_message=_build_message(payload),
            model=ReviewResult,
            ctx=ctx,
            max_tokens=800,
        )
        return enforce_policy(result, payload.results, self.deps.settings.review_min_score)


def _build_message(payload: ReviewInput) -> str:
    plan_lines = "\n".join(
        f"- {s.key}: {s.description} [{s.agent.value}/{s.tool or '-'}]" for s in payload.plan.steps
    )
    results = [
        {
            "key": r.key,
            "status": r.status.value,
            "tool": r.tool,
            "error": r.error,
            "output": _preview(r.output),
        }
        for r in payload.results
    ]
    parts = [
        tagged("goal", payload.goal),
        tagged("plan_summary", f"{payload.plan.summary}\n{plan_lines}"),
        tagged("revision", str(payload.revision)),
        tagged("step_results", wrap_untrusted(json.dumps(results), "step results")),
    ]
    if payload.previous_feedback:
        parts.append(tagged("previous_review_feedback", payload.previous_feedback))
    return "\n\n".join(parts)


def _preview(output: dict[str, Any] | None) -> str | None:
    """Short text of a step output; a report or findings are shown as text, not as JSON."""
    if output is None:
        return None
    if isinstance(output.get("report"), str):
        return str(output["report"])[:_REPORT_PREVIEW_CHARS]
    if isinstance(output.get("findings"), str):
        grounded = "grounded" if output.get("grounded") else "NOT grounded in any source"
        return f"({grounded}) {output['findings']}"[:_OUTPUT_PREVIEW_CHARS]
    return json.dumps(output, default=str)[:_OUTPUT_PREVIEW_CHARS]


def enforce_policy(
    result: ReviewResult, results: list[StepResult], min_score: float
) -> ReviewResult:
    """Apply the rules the model is not allowed to override (see module docstring)."""
    verdict = result.verdict
    reasons = list(result.reasons)
    score = result.score
    if verdict is ReviewVerdict.APPROVED:
        unfinished = [r.key for r in results if r.status is not StepStatus.SUCCEEDED]
        if unfinished:
            verdict = ReviewVerdict.NEEDS_REVISION
            reasons.append(f"[policy] steps did not succeed: {', '.join(unfinished)}")
        elif score < min_score:
            verdict = ReviewVerdict.NEEDS_REVISION
            reasons.append(f"[policy] score {score:.2f} is below the minimum {min_score:.2f}")
        if verdict is not ReviewVerdict.APPROVED:
            score = min(score, max(0.0, round(min_score - 0.01, 3)))
    feedback = result.feedback
    if verdict is ReviewVerdict.NEEDS_REVISION and not feedback.strip():
        feedback = "; ".join(reasons)[:2000]
    return ReviewResult(verdict=verdict, score=score, reasons=reasons[:10], feedback=feedback)
