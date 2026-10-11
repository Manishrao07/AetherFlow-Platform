"""PlannerAgent: turns a user goal into a validated, executable Plan.

Steps: (1) describe ONLY the tools this user may use, (2) ask the LLM for a JSON plan,
(3) validate it - schema, graph (no cycles), and a semantic check against the real tool
registry, (4) overwrite risk/approval flags from the tool's own metadata.
Why step 4: the model's claim "this step is low risk" is advice, not authority. The tool
declares its risk; the plan can only raise it, never lower it. So a prompt-injected plan cannot
smuggle a HIGH-risk tool past the approval gate by labelling it LOW.
The Planner has no tools of its own (least privilege): memories and feedback are passed in.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.base import AgentContext, AgentOutputError, LLMAgent, tagged
from app.agents.execution import ExecutionAgent
from app.agents.plan import Plan, PlanStep, higher_risk
from app.agents.research import WEB_TOOL, ResearchAgent, validate_step_inputs
from app.agents.retrieval import SEARCH_TOOL, RetrievalAgent
from app.agents.synthesizer import SynthesizerStepInputs
from app.agents.templating import substitute
from app.core.rbac import Principal
from app.core.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.models.enums import AgentName, RiskLevel, RunMode
from app.services.permissions import PERMISSIONS
from app.tools.base import ToolNotFoundError


class PlannerInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    goal: str = Field(min_length=3, max_length=4000)
    memories: list[str] = Field(default_factory=list, max_length=10)
    # Set when the Reviewer asked for a revision: the model sees what went wrong last time.
    feedback: str | None = Field(default=None, max_length=2000)
    previous_plan: Plan | None = None
    # Recent conversation (summary + latest messages) rendered as text; untrusted like memories.
    context: str | None = Field(default=None, max_length=12000)
    mode: RunMode = RunMode.STANDARD


class SubQuestions(BaseModel):
    """What the model returns in deep-research mode: the code builds the plan from it."""

    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=500)
    sub_questions: list[str] = Field(min_length=1, max_length=12)


_SYSTEM_TEMPLATE = """You are the Planner agent of a multi-agent workflow platform.
Turn the user's goal into a JSON plan. Reply with ONLY one JSON object, no prose.

Schema: {{"summary": string, "steps": [{{"key": "s1", "description": string,
"agent": "retrieval" | "execution", "tool": string, "inputs": object,
"depends_on": [step keys], "requires_approval": boolean, "risk_level": "LOW"|"MEDIUM"|"HIGH"}}]}}

Rules:
- At most {max_steps} steps. Step keys are lowercase like s1, s2.
- "retrieval" steps use the tool {search_tool}. "execution" steps use one of the tools below.
- Use ONLY the tools listed; each step's inputs must match that tool's input schema.
- To use an earlier step's output inside a text input, write {{{{step_key}}}} and list that step in
  depends_on. Independent steps should NOT depend on each other (they run in parallel).
- Mark risky actions (sending messages, changing data) with the tool's risk level.
- {untrusted_notice}

Available tools (JSON):
{catalogue}"""


_DEEP_SYSTEM_TEMPLATE = """You are the Planner agent of a multi-agent workflow platform, in
deep-research mode. Break the user's topic into {max_questions} distinct sub-questions that
together cover it (definition and background, main facts, benefits or applications, risks or
limitations, recent developments - choose what fits). If the topic compares things (A vs B),
give each side its own sub-questions plus one or two direct comparison questions. Each
sub-question must be answerable on its own, self-contained, and between 5 and 300 characters.
Reply with ONLY one JSON object: {{"summary": string, "sub_questions": [string]}}.
{untrusted_notice}"""


class PlannerAgent(LLMAgent[PlannerInput, Plan]):
    name = AgentName.PLANNER
    allowed_tools = frozenset()  # WHY none: planning must never have side effects.

    async def run(self, payload: PlannerInput, ctx: AgentContext) -> Plan:
        principal = ctx.principal
        if payload.mode is RunMode.DEEP:
            return await self._plan_deep(payload, ctx)
        plan = await self.complete_json(
            task="plan",
            user_message=_build_message(payload),
            model=Plan,
            ctx=ctx,
            max_tokens=2000,
            system=self._system_prompt_for(principal),
            check=lambda p: self._normalise(p, principal),
        )
        return plan

    async def _plan_deep(self, payload: PlannerInput, ctx: AgentContext) -> Plan:
        """Deep research: the model proposes sub-questions, CODE builds the plan around them.

        WHY code builds the graph: the shape is always the same (N independent research steps
        feeding one synthesis step), so asking a model to wire it would only add ways to fail.
        """
        principal = ctx.principal
        limit = self.deps.settings.deep_research_max_questions
        questions = await self.complete_json(
            task="plan_deep",
            user_message=_build_message(payload),
            model=SubQuestions,
            ctx=ctx,
            max_tokens=800,
            system=_DEEP_SYSTEM_TEMPLATE.format(
                max_questions=limit, untrusted_notice=UNTRUSTED_NOTICE
            ),
        )
        asked = list(dict.fromkeys(q.strip() for q in questions.sub_questions if q.strip()))[:limit]
        sources = self._research_sources(principal)
        steps = [
            PlanStep(
                key=f"s{i}",
                description=f"Research: {q}"[:300],
                agent=AgentName.RESEARCH,
                inputs={"question": q[:300], "sources": sources},
            )
            for i, q in enumerate(asked, start=1)
        ]
        steps.append(
            PlanStep(
                key=f"s{len(steps) + 1}",
                description="Write the cited research report",
                agent=AgentName.SYNTHESIZER,
                inputs={"topic": payload.goal},
                depends_on=[s.key for s in steps],
            )
        )
        try:
            return self._normalise(
                Plan(summary=questions.summary, steps=steps), principal, deep=True
            )
        except ValueError as exc:
            raise AgentOutputError(f"deep research plan is invalid: {exc}") from exc

    def _research_sources(self, principal: Principal) -> list[str]:
        """Source tools this user may use; web search only when it is configured."""
        usable = {
            name
            for name in self.deps.registry.names()
            if name in ResearchAgent.allowed_tools
            and PERMISSIONS.allows(principal.roles, self.deps.registry.get(name).permission)
        }
        if self.deps.settings.tavily_api_key is None:
            usable.discard(WEB_TOOL)  # WHY: skip a source that can only answer "no API key"
        return sorted(usable)

    def _available_tools(self, principal: Principal) -> list[str]:
        """Tools this user may use AND that some step agent is allowed to call."""
        usable = RetrievalAgent.allowed_tools | ExecutionAgent.allowed_tools
        return [
            name
            for name in self.deps.registry.names()
            if name in usable
            and PERMISSIONS.allows(principal.roles, self.deps.registry.get(name).permission)
        ]

    def _system_prompt_for(self, principal: Principal) -> str:
        catalogue = [
            {
                "name": spec["name"],
                "description": spec["description"],
                "risk_level": spec["risk_level"],
                "input_schema": spec["input_schema"],
            }
            for spec in self.deps.registry.specs()
            if spec["name"] in self._available_tools(principal)
        ]
        return _SYSTEM_TEMPLATE.format(
            max_steps=self.deps.settings.max_plan_steps,
            search_tool=SEARCH_TOOL,
            untrusted_notice=UNTRUSTED_NOTICE,
            catalogue=json.dumps(catalogue, separators=(",", ":")),
        )

    def _check_deep_step(self, step: PlanStep, principal: Principal) -> PlanStep:
        """Validate a research/synthesizer step. Risk is LOW: neither can cause a side effect."""
        if step.tool is not None:
            raise ValueError(f"step '{step.key}': {step.agent.value} steps take no tool")
        if step.agent is AgentName.RESEARCH:
            allowed = set(self._research_sources(principal))
            denied = sorted(set(validate_step_inputs(step.inputs).sources) - allowed)
            if denied:
                raise ValueError(f"step '{step.key}': sources not available: {', '.join(denied)}")
        else:
            try:
                SynthesizerStepInputs.model_validate(step.inputs)
            except ValidationError as exc:
                raise ValueError(f"step '{step.key}': invalid synthesizer inputs") from exc
        return step.model_copy(update={"risk_level": RiskLevel.LOW, "requires_approval": False})

    def _normalise(self, plan: Plan, principal: Principal, *, deep: bool = False) -> Plan:
        """Semantic validation against the real registry; returns the plan with trusted flags."""
        if len(plan.steps) > self.deps.settings.max_plan_steps:
            raise ValueError(f"plan has too many steps (max {self.deps.settings.max_plan_steps})")
        usable = set(self._available_tools(principal))
        steps = []
        for step in plan.steps:
            if step.agent in {AgentName.RESEARCH, AgentName.SYNTHESIZER}:
                if not deep:
                    raise ValueError(f"step '{step.key}': {step.agent.value} steps are deep-only")
                steps.append(self._check_deep_step(step, principal))
                continue
            agent_class = RetrievalAgent if step.agent is AgentName.RETRIEVAL else ExecutionAgent
            tool_name = step.tool or (SEARCH_TOOL if step.agent is AgentName.RETRIEVAL else None)
            if tool_name is None:
                raise ValueError(f"step '{step.key}' needs a tool")
            try:
                tool = self.deps.registry.get(tool_name)
            except ToolNotFoundError as exc:
                raise ValueError(f"step '{step.key}': unknown tool '{tool_name}'") from exc
            if tool_name not in agent_class.allowed_tools:
                raise ValueError(
                    f"step '{step.key}': the {step.agent.value} agent may not use '{tool_name}'"
                )
            if tool_name not in usable:
                raise ValueError(f"step '{step.key}': you are not permitted to use '{tool_name}'")
            try:
                # dummy placeholders so {{s1}} passes length/type checks before s1 has run
                tool.input_model.model_validate(substitute(step.inputs, {}, dummy=True))
            except ValidationError as exc:
                fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
                raise ValueError(
                    f"step '{step.key}': invalid inputs for '{tool_name}' ({fields})"
                ) from exc
            steps.append(
                step.model_copy(
                    update={
                        "tool": tool_name,
                        "risk_level": higher_risk(step.risk_level, tool.risk),
                        "requires_approval": step.requires_approval or tool.requires_approval,
                    }
                )
            )
        return plan.model_copy(update={"steps": steps})


def _build_message(payload: PlannerInput) -> str:
    parts = [tagged("goal", payload.goal)]
    if payload.context:
        parts.append(
            tagged("conversation_context", wrap_untrusted(payload.context, "conversation"))
        )
    if payload.memories:
        remembered = "\n".join(f"- {m}" for m in payload.memories)
        parts.append(tagged("user_memories", wrap_untrusted(remembered, "long-term memory")))
    if payload.feedback:
        parts.append(tagged("reviewer_feedback", wrap_untrusted(payload.feedback, "review")))
    if payload.previous_plan:
        parts.append(tagged("previous_plan", payload.previous_plan.model_dump_json()))
    return "\n\n".join(parts)
