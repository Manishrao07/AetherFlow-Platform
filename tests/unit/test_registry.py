"""Every gate in the tool registry, in order, with stub tools (no DB)."""

import asyncio
import uuid
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.core.rbac import Principal
from app.models.enums import RiskLevel
from app.services.permissions import PERMISSIONS
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
from app.tools.registry import ToolRegistry
from tests.conftest import make_settings


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class EchoTool(Tool):
    name = "echo"
    description = "echo"
    input_model = EchoInput
    permission = "workflow:create"

    async def run(self, args: EchoInput, ctx: ToolContext) -> dict[str, Any]:
        return {"text": args.text}


class RiskyTool(EchoTool):
    name = "risky"
    risk = RiskLevel.HIGH


class SlowTool(EchoTool):
    name = "slow"
    timeout_seconds = 0.05

    async def run(self, args: EchoInput, ctx: ToolContext) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {}


class BigTool(EchoTool):
    name = "big"

    async def run(self, args: EchoInput, ctx: ToolContext) -> dict[str, Any]:
        return {"blob": "x" * 5000}


class FlakyExternal(EchoTool):
    name = "flaky"
    uses_breaker = True

    async def run(self, args: EchoInput, ctx: ToolContext) -> dict[str, Any]:
        raise ToolError("upstream down", retryable=True)


def ctx(*roles: str, approved: bool = False) -> ToolContext:
    return ToolContext(
        principal=Principal(uuid.uuid4(), "u@x.io", frozenset(roles)),
        session_factory=None,  # type: ignore[arg-type]
        settings=make_settings(),
        approved=approved,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry(PERMISSIONS, max_output_chars=1000, breaker_threshold=2)
    for tool in (EchoTool(), RiskyTool(), SlowTool(), BigTool(), FlakyExternal()):
        reg.register(tool)
    return reg


async def call(reg: ToolRegistry, name: str, inputs: dict[str, Any], c: ToolContext, **kw: Any):
    kw.setdefault("plan_tools", [name])
    kw.setdefault("agent_allowlist", [name])
    return await reg.invoke(name, inputs, c, **kw)


async def test_happy_path(registry: ToolRegistry) -> None:
    result = await call(registry, "echo", {"text": "hi"}, ctx("operator"))
    assert result.output == {"text": "hi"} and not result.truncated


async def test_gate1_unknown_tool(registry: ToolRegistry) -> None:
    with pytest.raises(ToolNotFoundError):
        await call(
            registry, "nope", {}, ctx("operator"), plan_tools=["nope"], agent_allowlist=["nope"]
        )


async def test_gate2_tool_must_be_in_the_plan(registry: ToolRegistry) -> None:
    with pytest.raises(ToolNotAllowedError, match="plan"):
        await call(registry, "echo", {"text": "x"}, ctx("operator"), plan_tools=["calculator"])


async def test_gate3_tool_must_be_in_agent_allowlist(registry: ToolRegistry) -> None:
    with pytest.raises(ToolNotAllowedError, match="agent"):
        await call(registry, "echo", {"text": "x"}, ctx("operator"), agent_allowlist=[])


async def test_gate4_role_must_hold_permission(registry: ToolRegistry) -> None:
    with pytest.raises(ToolNotAllowedError, match="role"):
        await call(registry, "echo", {"text": "x"}, ctx("viewer"))
    with pytest.raises(ToolNotAllowedError):
        await call(registry, "echo", {"text": "x"}, ctx())  # no roles at all


async def test_gate5_validation_rejects_bad_and_extra_fields(registry: ToolRegistry) -> None:
    for bad in ({}, {"text": 5}, {"text": "ok", "injected": "rm -rf"}):
        with pytest.raises(ToolValidationError) as exc:
            await call(registry, "echo", bad, ctx("operator"))
        assert exc.value.details and not exc.value.retryable


async def test_gate6_high_risk_needs_human_approval(registry: ToolRegistry) -> None:
    with pytest.raises(ApprovalRequiredError):
        await call(registry, "risky", {"text": "x"}, ctx("operator"))
    ok = await call(registry, "risky", {"text": "x"}, ctx("operator", approved=True))
    assert ok.output == {"text": "x"}


async def test_gate7_timeout_is_retryable_tool_error(registry: ToolRegistry) -> None:
    with pytest.raises(ToolTimeoutError) as exc:
        await call(registry, "slow", {"text": "x"}, ctx("operator"))
    assert exc.value.retryable


async def test_oversized_output_is_truncated(registry: ToolRegistry) -> None:
    result = await call(registry, "big", {"text": "x"}, ctx("operator"))
    assert result.truncated and result.output["truncated"] is True
    assert len(result.output["preview"]) == 1000


async def test_external_tool_breaker_opens_and_fails_fast(registry: ToolRegistry) -> None:
    for _ in range(2):
        with pytest.raises(ToolError, match="upstream down"):
            await call(registry, "flaky", {"text": "x"}, ctx("operator"))
    with pytest.raises(ToolError, match="temporarily unavailable") as exc:
        await call(registry, "flaky", {"text": "x"}, ctx("operator"))
    assert exc.value.retryable


def test_registry_housekeeping(registry: ToolRegistry) -> None:
    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoTool())
    assert registry.names() == sorted(["echo", "risky", "slow", "big", "flaky"])
    spec = next(s for s in registry.specs() if s["name"] == "risky")
    assert spec["requires_approval"] and spec["risk_level"] == "HIGH"
    assert spec["input_schema"]["properties"]["text"]["type"] == "string"
