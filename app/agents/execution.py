"""ExecutionAgent: runs ONE plan step by calling its tool through the tool layer.

Why it does not call the LLM: the Planner already chose the tool and its inputs, and a human
may have approved exactly that. Letting a model improvise at execution time would make the
approved plan meaningless. So execution is deterministic: render inputs -> tool executor.
Safety comes from the registry gates (tool in plan, in this agent's allow-list, user may use it,
inputs valid, approved if HIGH risk), which the executor applies on every call.
Why ``approved`` is an input: only the orchestrator, after a recorded human decision, sets it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.base import Agent, AgentContext
from app.agents.templating import substitute
from app.models.enums import AgentName
from app.tools.base import ToolContext, ToolError


class ExecutionInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    tool: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    attempt: int = Field(default=1, ge=1)
    plan_tools: frozenset[str]
    # Outputs of the steps this one depends on, keyed by step key (for {{key}} placeholders).
    upstream: dict[str, dict[str, Any]] = Field(default_factory=dict)
    approved: bool = False


class ExecutionOutput(BaseModel):
    tool: str
    output: dict[str, Any]
    duration_ms: int
    truncated: bool = False
    replayed: bool = False


class ExecutionAgent(Agent[ExecutionInput, ExecutionOutput]):
    name = AgentName.EXECUTION
    allowed_tools = frozenset(
        {
            "calculator",
            "create_ticket",
            "http_fetch",
            "memory_read",
            "memory_write",
            "search_knowledge_base",
            "send_notification",
        }
    )

    async def run(self, payload: ExecutionInput, ctx: AgentContext) -> ExecutionOutput:
        try:
            inputs = substitute(payload.inputs, payload.upstream)
        except KeyError as exc:
            # A scheduler bug, not a transient fault: retrying cannot help.
            raise ToolError(f"missing output of step {exc}", retryable=False) from exc
        result = await self.deps.executor.execute(
            payload.tool,
            inputs,
            ToolContext(
                principal=ctx.principal,
                session_factory=self.deps.session_factory,
                settings=self.deps.settings,
                run_id=ctx.run_id,
                step_id=ctx.step_id,
                # WHY the step id: a retry of the same step reuses it, so side-effecting tools
                # (ticket, notification) can recognise "already done".
                side_effect_key=str(ctx.step_id),
                approved=payload.approved,
            ),
            attempt=payload.attempt,
            plan_tools=payload.plan_tools,
            agent_allowlist=self.allowed_tools,
        )
        return ExecutionOutput(
            tool=result.tool,
            output=result.output,
            duration_ms=result.duration_ms,
            truncated=result.truncated,
            replayed=result.replayed,
        )
