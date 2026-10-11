"""The Plan: the validated contract between the Planner and the rest of the system.

What: ``Plan`` / ``PlanStep`` Pydantic models plus graph checks (unique keys, known
dependencies, no cycles) and helpers (topological layers, max risk, approval need).
Why a strict schema: the plan is LLM output, and LLM output is untrusted. Everything downstream
(approval, scheduling, execution) trusts only a plan that passed this validation.
Why graph checks here and not in the orchestrator: an invalid DAG (cycle, dangling dependency)
would deadlock a run; rejecting it at planning time costs one repair attempt instead.
Connects to: ``planner.py`` (produces it), the orchestrator (stores steps + dependencies).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.templating import placeholders_in
from app.models.enums import AgentName, RiskLevel

_RISK_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
# WHY these four: the Planner plans and the Reviewer reviews the whole run; neither is a
# step that executes work. Research and synthesizer steps exist only in deep-research plans.
STEP_AGENTS = frozenset(
    {AgentName.RETRIEVAL, AgentName.EXECUTION, AgentName.RESEARCH, AgentName.SYNTHESIZER}
)


def higher_risk(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    """Return the more dangerous of two risk levels."""
    return a if _RISK_ORDER[a] >= _RISK_ORDER[b] else b


class PlanStep(BaseModel):
    """One unit of work. ``extra='forbid'`` so unexpected fields are an error, not ignored."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$", examples=["s1"])
    description: str = Field(min_length=1, max_length=300)
    agent: AgentName
    tool: str | None = Field(default=None, max_length=100)
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    requires_approval: bool = False
    risk_level: RiskLevel = RiskLevel.LOW

    @model_validator(mode="after")
    def _agent_is_a_step_agent(self) -> PlanStep:
        if self.agent not in STEP_AGENTS:
            raise ValueError(
                f"a step's agent must be one of {sorted(a.value for a in STEP_AGENTS)}"
            )
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError(f"step '{self.key}' lists the same dependency twice")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=500)
    steps: list[PlanStep] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_graph(self) -> Plan:
        keys = [s.key for s in self.steps]
        if len(set(keys)) != len(keys):
            raise ValueError("step keys must be unique")
        known = set(keys)
        for step in self.steps:
            if step.key in step.depends_on:
                raise ValueError(f"step '{step.key}' depends on itself")
            missing = set(step.depends_on) - known
            if missing:
                raise ValueError(f"step '{step.key}' depends on unknown steps {sorted(missing)}")
            # WHY: {{key}} placeholders read another step's output, so that step must have
            # finished first - the dependency has to be declared, never implied.
            undeclared = placeholders_in(step.inputs) - set(step.depends_on)
            if undeclared:
                raise ValueError(
                    f"step '{step.key}' uses outputs of {sorted(undeclared)} "
                    "without listing them in depends_on"
                )
        self.layers()  # raises ValueError on a cycle
        return self

    def layers(self) -> list[list[str]]:
        """Group step keys into waves; steps in one wave can run concurrently.

        Kahn's algorithm. A cycle means some steps never become ready -> ValueError.
        """
        remaining = {s.key: set(s.depends_on) for s in self.steps}
        waves: list[list[str]] = []
        done: set[str] = set()
        while remaining:
            ready = sorted(k for k, deps in remaining.items() if deps <= done)
            if not ready:
                raise ValueError(f"dependency cycle among steps {sorted(remaining)}")
            waves.append(ready)
            done.update(ready)
            for key in ready:
                del remaining[key]
        return waves

    @property
    def tools(self) -> frozenset[str]:
        """Every tool the plan uses - the allow-list the tool registry checks against.

        A research step has no single tool: it names its sources in ``inputs["sources"]``, and
        those count as "in the plan" so the registry's plan gate still applies to them.
        """
        used = {s.tool for s in self.steps if s.tool}
        for step in self.steps:
            sources = step.inputs.get("sources")
            if step.agent is AgentName.RESEARCH and isinstance(sources, list):
                used.update(str(t) for t in sources)
        return frozenset(used)

    @property
    def max_risk(self) -> RiskLevel:
        risk = RiskLevel.LOW
        for step in self.steps:
            risk = higher_risk(risk, step.risk_level)
        return risk

    @property
    def needs_approval(self) -> bool:
        return any(s.requires_approval for s in self.steps)
