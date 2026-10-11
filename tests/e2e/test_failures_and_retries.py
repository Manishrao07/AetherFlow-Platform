"""Retries, permanent failures, the retry-step endpoint and graceful LLM failures."""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.llm.base import LLMAuthError, LLMRequest, LLMServerError
from tests.e2e.helpers import (
    as_user,
    flow_app,
    make_flaky,
    start,
    step_by_key,
    timeline,
    wait_status,
)

CALC = "Calculate 2 + 3 for the report"


async def test_transient_tool_failure_is_retried_until_it_succeeds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        calls = make_flaky(http.app.state.tool_registry.get("calculator"), fail_times=2)  # type: ignore[attr-defined]
        run = await start(http, op, CALC)
        await wait_status(http, op, run["id"], "COMPLETED")
        tl = await timeline(http, op, run["id"])
        step = step_by_key(tl, "s2")
        assert step["status"] == "SUCCEEDED" and step["attempts"] == 3 and calls["n"] == 3
        assert [e["type"] for e in tl["events"]].count("step.retry") == 2
        statuses = [t["status"] for t in tl["tool_executions"] if t["tool_name"] == "calculator"]
        assert statuses == ["FAILED", "FAILED", "SUCCEEDED"]


async def test_permanent_failure_fails_the_run_and_retry_step_recovers_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, default_max_revisions=0) as http:
        op = await as_user(http, "operator")
        # fails exactly the 3 allowed attempts, then works again
        make_flaky(http.app.state.tool_registry.get("calculator"), fail_times=3)  # type: ignore[attr-defined]
        run = await start(http, op, CALC)
        failed = await wait_status(http, op, run["id"], "FAILED")
        assert failed["review_verdict"] == "NEEDS_REVISION"
        tl = await timeline(http, op, run["id"])
        step = step_by_key(tl, "s2")
        assert step["status"] == "FAILED" and step["attempts"] == 3 and step["error"]

        wrong = await http.post(
            f"/api/v1/workflows/{run['id']}/steps/{step_by_key(tl, 's1')['id']}/retry", headers=op
        )
        assert wrong.status_code == 409  # s1 succeeded; only FAILED steps can be retried
        retried = await http.post(
            f"/api/v1/workflows/{run['id']}/steps/{step['id']}/retry", headers=op
        )
        assert retried.status_code == 202, retried.text
        await wait_status(http, op, run["id"], "COMPLETED")
        tl = await timeline(http, op, run["id"])
        assert step_by_key(tl, "s2")["status"] == "SUCCEEDED"
        assert step_by_key(tl, "s2")["attempts"] == 4


async def test_non_retryable_error_is_not_retried(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, default_max_revisions=0) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, "Calculate 1 / 0 for the report")
        await wait_status(http, op, run["id"], "FAILED")
        step = step_by_key(await timeline(http, op, run["id"]), "s2")
        assert step["status"] == "FAILED" and step["attempts"] == 1


async def test_retry_step_requires_a_failed_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, CALC)
        await wait_status(http, op, run["id"], "COMPLETED")
        step = step_by_key(await timeline(http, op, run["id"]), "s2")
        response = await http.post(
            f"/api/v1/workflows/{run['id']}/steps/{step['id']}/retry", headers=op
        )
        assert response.status_code == 409
        missing = await http.post(
            f"/api/v1/workflows/{run['id']}/steps/00000000-0000-0000-0000-000000000000/retry",
            headers=op,
        )
        assert missing.status_code == 404


async def test_transient_llm_failure_in_a_step_is_retried(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, llm_max_attempts=1) as http:
        op = await as_user(http, "operator")
        provider = http.app.state.llm.provider  # type: ignore[attr-defined]
        real = provider._handlers["retrieve_answer"]
        calls = {"n": 0}

        def flaky(request: LLMRequest) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise LLMServerError("503 from provider")  # retryable at step level
            return str(real(request))

        provider.register("retrieve_answer", flaky)
        run = await start(http, op, "What is the refund window for digital goods?")
        await wait_status(http, op, run["id"], "COMPLETED")
        assert step_by_key(await timeline(http, op, run["id"]), "s1")["attempts"] >= 2


async def test_unplannable_goal_fails_the_run_cleanly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        http.app.state.llm.provider.register("plan", lambda r: "no json here")  # type: ignore[attr-defined]
        run = await start(http, op, "Do something impossible please")
        failed = await wait_status(http, op, run["id"], "FAILED")
        assert "planning failed" in failed["error"].lower()
        tl = await timeline(http, op, run["id"])
        assert tl["steps"] == []


async def test_llm_outage_fails_the_run_instead_of_hanging(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        http.app.state.llm.provider.fail_next(*[LLMAuthError("bad key")] * 5)  # type: ignore[attr-defined]
        run = await start(http, op, "What is the refund window for digital goods?")
        failed = await wait_status(http, op, run["id"], "FAILED")
        assert failed["error"]
        assert "bad key" not in json.dumps(failed.get("result") or {})


def _plan(steps: list[dict[str, Any]]) -> str:
    return json.dumps({"summary": "custom", "steps": steps})
