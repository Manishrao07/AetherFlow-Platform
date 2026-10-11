"""Pure helpers of the orchestrator: error classification and result summaries."""

import pytest

from app.agents.base import AgentOutputError
from app.agents.plan import Plan
from app.core.llm.base import (
    CircuitOpenError,
    LLMAuthError,
    LLMRateLimitError,
    LLMServerError,
)
from app.models import WorkflowStep
from app.models.enums import AgentName, StepStatus
from app.orchestrator.engine import classify_error, summarise_results
from app.services.workflows import _dependents
from app.tools.base import ToolError, ToolTimeoutError, ToolValidationError


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (ToolTimeoutError("slow"), True),
        (ToolError("permanent"), False),
        (ToolError("transient", retryable=True), True),
        (ToolValidationError("bad input", [{"field": "x", "message": "m"}]), False),
        (LLMRateLimitError("429"), True),
        (LLMServerError("503"), True),
        (LLMAuthError("401"), False),
        (CircuitOpenError("open"), True),
        (AgentOutputError("invalid json"), True),
        (ValueError("bug"), False),
    ],
)
def test_error_classification(error: Exception, retryable: bool) -> None:
    assert classify_error(error)[0] is retryable


def test_error_messages_are_redacted_and_bounded() -> None:
    _, message = classify_error(
        ToolError("failed with key sk-ant-api03-" + "a" * 60 + " " + "x" * 900)
    )
    assert "sk-ant-api03" not in message and len(message) <= 500


def test_validation_error_message_lists_the_fields() -> None:
    _, message = classify_error(
        ToolValidationError("bad", [{"field": "recipient", "message": "no"}])
    )
    assert "recipient" in message


def plan() -> Plan:
    return Plan.model_validate(
        {
            "summary": "Do things",
            "steps": [
                {"key": "a", "description": "first", "agent": "execution", "tool": "calculator",
                 "inputs": {"expression": "1+1"}},
                {"key": "b", "description": "second", "agent": "execution", "tool": "calculator",
                 "inputs": {"expression": "2+2"}, "depends_on": ["a"]},
                {"key": "c", "description": "third", "agent": "execution", "tool": "calculator",
                 "inputs": {"expression": "{{b}}"}, "depends_on": ["b"]},
                {"key": "d", "description": "loner", "agent": "execution", "tool": "calculator",
                 "inputs": {"expression": "3+3"}},
            ],
        }
    )  # fmt: skip


def test_dependents_are_transitive() -> None:
    assert _dependents(plan(), "a") == {"b", "c"}
    assert _dependents(plan(), "d") == set()


def test_summary_lists_outputs_and_errors() -> None:
    def step(key: str, output: dict | None, error: str | None = None) -> WorkflowStep:
        return WorkflowStep(
            step_key=key, agent=AgentName.EXECUTION, status=StepStatus.SUCCEEDED,
            output=output, error=error,
        )  # fmt: skip

    text = summarise_results(
        plan(),
        [
            step("a", {"answer": "two"}),
            step("b", None, "boom"),
            step("c", None),
            step("d", {"result": 6}),
        ],
    )
    assert text.startswith("Do things")
    assert "- first: two" in text and "- second: boom" in text and "- third: -" in text
    assert "result: 6" in text
