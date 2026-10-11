"""Tool registry: the single gate every tool call must pass through.

Gates, in order (a call stops at the first failure):
 1. the tool exists
 2. the tool is in the approved PLAN        (a model cannot invent extra tool calls)
 3. the tool is in the calling AGENT's allow-list   (least privilege per agent)
 4. the USER's role grants the tool's permission    (RBAC at tool level)
 5. the input validates against the tool's schema   (reject malformed/unknown fields)
 6. HIGH-risk tools have been approved by a human
 7. run with a timeout (+ circuit breaker for external tools), cap the output size

Why one choke point: prompt-injected instructions can only ever reach tools by passing all
seven checks, and the checks are written once and tested once.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.core.llm.base import CircuitOpenError
from app.core.llm.circuit_breaker import CircuitBreaker
from app.core.observability.metrics import BREAKERS
from app.core.rbac import PermissionMap
from app.tools.base import (
    ApprovalRequiredError,
    Tool,
    ToolContext,
    ToolError,
    ToolNotAllowedError,
    ToolNotFoundError,
    ToolTimeoutError,
    ToolValidationError,
)


@dataclass(frozen=True)
class ToolResult:
    tool: str
    output: dict[str, Any]
    duration_ms: int
    truncated: bool = False
    replayed: bool = False  # True when returned from a previous identical execution


class ToolRegistry:
    def __init__(
        self,
        permissions: PermissionMap,
        *,
        max_output_chars: int = 20_000,
        breaker_threshold: int = 5,
        breaker_recovery_seconds: float = 30.0,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._permissions = permissions
        self._max_output = max_output_chars
        self._breaker_threshold = breaker_threshold
        self._breaker_recovery = breaker_recovery_seconds

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' already registered")
        self._tools[tool.name] = tool
        if tool.uses_breaker:
            breaker = CircuitBreaker(
                f"tool:{tool.name}",
                failure_threshold=self._breaker_threshold,
                recovery_seconds=self._breaker_recovery,
                should_count=lambda exc: isinstance(exc, ToolError) and exc.retryable,
            )
            self._breakers[tool.name] = breaker
            BREAKERS.register(breaker.name, lambda: breaker.state.value)  # exported as a gauge

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool '{name}'")
        return tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [self._tools[name].spec() for name in self.names()]

    def check_access(
        self,
        name: str,
        ctx: ToolContext,
        *,
        plan_tools: Iterable[str],
        agent_allowlist: Iterable[str],
    ) -> Tool:
        """Run gates 1-4 and return the tool (raises ``ToolError`` subclasses otherwise)."""
        tool = self.get(name)
        if name not in set(plan_tools):
            raise ToolNotAllowedError(f"tool '{name}' is not part of the approved plan")
        if name not in set(agent_allowlist):
            raise ToolNotAllowedError(f"tool '{name}' is not allowed for this agent")
        if not self._permissions.allows(ctx.principal.roles, tool.permission):
            raise ToolNotAllowedError(f"your role does not permit using '{name}'")
        return tool

    async def invoke(
        self,
        name: str,
        raw_inputs: dict[str, Any],
        ctx: ToolContext,
        *,
        plan_tools: Iterable[str],
        agent_allowlist: Iterable[str],
    ) -> ToolResult:
        tool = self.check_access(name, ctx, plan_tools=plan_tools, agent_allowlist=agent_allowlist)
        try:
            args = tool.input_model.model_validate(raw_inputs)
        except ValidationError as exc:
            raise ToolValidationError(
                f"invalid input for '{name}'",
                [
                    {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                    for e in exc.errors()
                ],
            ) from exc
        if tool.requires_approval and not ctx.approved:
            raise ApprovalRequiredError(f"tool '{name}' requires human approval")

        started = time.perf_counter()
        output = await self._run_guarded(tool, args, ctx)
        output, truncated = self._cap(output)
        return ToolResult(name, output, int((time.perf_counter() - started) * 1000), truncated)

    async def _run_guarded(self, tool: Tool, args: Any, ctx: ToolContext) -> dict[str, Any]:
        async def attempt() -> dict[str, Any]:
            try:
                async with asyncio.timeout(tool.timeout_seconds):
                    return await tool.run(args, ctx)
            except TimeoutError as exc:
                raise ToolTimeoutError(
                    f"tool '{tool.name}' timed out after {tool.timeout_seconds}s"
                ) from exc

        breaker = self._breakers.get(tool.name)
        if breaker is None:
            return await attempt()
        try:
            return await breaker.call(attempt)
        except CircuitOpenError as exc:
            # retryable: the orchestrator's backoff gives the dependency time to recover
            raise ToolError(f"tool '{tool.name}' temporarily unavailable", retryable=True) from exc

    def _cap(self, output: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Bound output size so one tool cannot flood the prompt or the database."""
        text = json.dumps(output, default=str)
        if len(text) <= self._max_output:
            return output, False
        return {"truncated": True, "preview": text[: self._max_output]}, True
