"""Plan model: schema rules and graph validation."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.plan import Plan, higher_risk
from app.models.enums import RiskLevel


def step(key: str, deps: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    base = {
        "key": key, "description": f"do {key}", "agent": "execution", "tool": "calculator",
        "inputs": {"expression": "1+1"}, "depends_on": deps or [],
    }  # fmt: skip
    return {**base, **extra}


def plan(*steps: dict[str, Any]) -> Plan:
    return Plan.model_validate({"summary": "test plan", "steps": list(steps)})


def test_valid_plan_layers_group_independent_steps() -> None:
    p = plan(step("a"), step("b"), step("c", ["a", "b"]), step("d", ["c"]))
    assert p.layers() == [["a", "b"], ["c"], ["d"]]


def test_tools_risk_and_approval_summaries() -> None:
    p = plan(
        step("a"),
        step("b", tool="send_notification", risk_level="HIGH", requires_approval=True),
    )
    assert p.tools == {"calculator", "send_notification"}
    assert p.max_risk is RiskLevel.HIGH
    assert p.needs_approval
    assert not plan(step("a")).needs_approval


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([step("a"), step("a")], "unique"),
        ([step("a", ["a"])], "itself"),
        ([step("a", ["zzz"])], "unknown steps"),
        ([step("a", ["b"]), step("b", ["a"])], "cycle"),
        ([step("a", ["b", "c"]), step("b", ["c"]), step("c", ["a"])], "cycle"),
        ([step("a", ["x", "x"]), step("x")], "twice"),
        ([step("a", agent="review")], "must be one of"),
        ([step("a", agent="planner")], "must be one of"),
    ],
)
def test_invalid_graphs_are_rejected(steps: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        plan(*steps)


def test_placeholder_requires_declared_dependency() -> None:
    with pytest.raises(ValidationError, match="without listing"):
        plan(step("a"), step("b", inputs={"expression": "{{a}}"}))
    ok = plan(step("a"), step("b", ["a"], inputs={"expression": "{{a}}"}))
    assert ok.layers() == [["a"], ["b"]]


def test_schema_is_strict() -> None:
    with pytest.raises(ValidationError):
        plan(step("a", surprise=1))
    with pytest.raises(ValidationError):
        plan(step("Bad Key"))
    with pytest.raises(ValidationError):
        Plan.model_validate({"summary": "x", "steps": []})
    with pytest.raises(ValidationError):
        Plan.model_validate({"summary": "x", "steps": [step("a")], "extra": True})


def test_higher_risk() -> None:
    assert higher_risk(RiskLevel.LOW, RiskLevel.HIGH) is RiskLevel.HIGH
    assert higher_risk(RiskLevel.MEDIUM, RiskLevel.LOW) is RiskLevel.MEDIUM
    assert higher_risk(RiskLevel.MEDIUM, RiskLevel.MEDIUM) is RiskLevel.MEDIUM
