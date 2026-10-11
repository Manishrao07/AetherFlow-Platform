"""Revision loop, review rejection, cancellation, parallel DAG execution and dependencies."""

import asyncio
import json
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.tools.base import ToolError
from tests.e2e.helpers import (
    as_user,
    flow_app,
    start,
    step_by_key,
    timeline,
    wait_for,
    wait_status,
)

GOAL = "What is the refund window for digital goods?"


def calc_step(key: str, expression: str, depends_on: list[str] | None = None) -> dict[str, Any]:
    return {
        "key": key, "description": f"calculate {expression}", "agent": "execution",
        "tool": "calculator", "inputs": {"expression": expression},
        "depends_on": depends_on or [], "requires_approval": False, "risk_level": "LOW",
    }  # fmt: skip


def custom_plan(*steps: dict[str, Any]) -> Any:
    body = json.dumps({"summary": "custom plan", "steps": list(steps)})
    return lambda request: body


def slow_calculator(http: Any, seconds: float) -> None:
    tool = http.app.state.tool_registry.get("calculator")
    real = tool.run

    async def run(args: Any, ctx: Any) -> dict[str, Any]:
        await asyncio.sleep(seconds)
        return await real(args, ctx)  # type: ignore[no-any-return]

    tool.run = run


def fail_expression(http: Any, expression: str) -> None:
    """Make the calculator fail permanently (non-retryably) for one expression."""
    tool = http.app.state.tool_registry.get("calculator")
    real = tool.run

    async def run(args: Any, ctx: Any) -> dict[str, Any]:
        if args.expression == expression:
            raise ToolError("boom", retryable=False)
        return await real(args, ctx)  # type: ignore[no-any-return]

    tool.run = run


async def test_review_can_send_the_run_back_to_planning(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, f"{GOAL} [force-revision]")
        done = await wait_status(http, op, run["id"], "COMPLETED")
        assert done["revision_count"] == 1
        tl = await timeline(http, op, run["id"])
        verdicts = [e["data"]["verdict"] for e in tl["events"] if e["type"] == "review.verdict"]
        assert verdicts == ["NEEDS_REVISION", "APPROVED"]
        assert {s["revision"] for s in tl["steps"]} == {0, 1}  # old attempt stays in history
        assert step_by_key(tl, "s1", revision=0)["status"] == "SUCCEEDED"


async def test_revisions_are_capped(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with flow_app(session_factory, default_max_revisions=0) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, f"{GOAL} [force-revision]")
        failed = await wait_status(http, op, run["id"], "FAILED")
        assert failed["revision_count"] == 0 and "revision" in failed["error"].lower()


async def test_rejected_output_fails_the_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, f"{GOAL} [force-reject]")
        failed = await wait_status(http, op, run["id"], "FAILED")
        assert "REJECTED" in failed["error"] and failed["review_verdict"] == "REJECTED"


async def test_cancel_stops_a_running_step(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        slow_calculator(http, 5.0)
        run = await start(http, op, "Calculate 2 + 3 please")

        async def calculating() -> bool:
            tl = await timeline(http, op, run["id"])
            return any(s["step_key"] == "s2" and s["status"] == "RUNNING" for s in tl["steps"])

        await wait_for(calculating, what="step s2 running")
        approver = await as_user(http, "approver")
        forbidden = await http.post(f"/api/v1/workflows/{run['id']}/cancel", headers=approver)
        assert forbidden.status_code == 403
        cancelled = await http.post(f"/api/v1/workflows/{run['id']}/cancel", headers=op)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
        again = await http.post(f"/api/v1/workflows/{run['id']}/cancel", headers=op)
        assert again.status_code == 409

        async def settled() -> bool:
            tl = await timeline(http, op, run["id"])
            return all(s["status"] != "RUNNING" for s in tl["steps"])

        await wait_for(settled, what="running step to be stopped")
        run_after = await http.get(f"/api/v1/workflows/{run['id']}", headers=op)
        assert run_after.json()["status"] == "CANCELLED"  # nothing resurrected it


async def test_independent_steps_run_in_parallel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        slow_calculator(http, 0.5)
        http.app.state.llm.provider.register(  # type: ignore[attr-defined]
            "plan", custom_plan(calc_step("a", "1 + 1"), calc_step("b", "2 + 2"))
        )
        run = await start(http, op, "Do two independent calculations")
        await wait_status(http, op, run["id"], "COMPLETED")
        tl = await timeline(http, op, run["id"])
        a, b = step_by_key(tl, "a"), step_by_key(tl, "b")
        started = [datetime.fromisoformat(s["started_at"]) for s in (a, b)]
        finished = [datetime.fromisoformat(s["finished_at"]) for s in (a, b)]
        assert max(started) < min(finished)  # the two intervals overlap


async def test_dependents_wait_and_a_failed_dependency_skips_them(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, default_max_revisions=0) as http:
        op = await as_user(http, "operator")
        fail_expression(http, "1 + 1")
        http.app.state.llm.provider.register(  # type: ignore[attr-defined]
            "plan",
            custom_plan(
                calc_step("a", "1 + 1"),
                calc_step("b", "2 + 2", depends_on=["a"]),
                {**calc_step("c", "3 + 3"), "tool": "calculator"},
            ),
        )
        run = await start(http, op, "Dependency chain with one failure")
        await wait_status(http, op, run["id"], "FAILED")
        tl = await timeline(http, op, run["id"])
        by_status = {s["step_key"]: s["status"] for s in tl["steps"]}
        assert by_status["a"] == "FAILED" and by_status["b"] == "SKIPPED"
        assert by_status["c"] == "SUCCEEDED"  # unrelated work still finished
