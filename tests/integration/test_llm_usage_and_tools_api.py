"""LLM usage persisted to llm_calls; /tools and /readyz on the real app."""

from decimal import Decimal

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.llm.base import LLMMessage, LLMRequest, LLMServerError
from app.core.llm.client import CallContext
from app.models import LLMCall
from app.services.llm_usage import DbUsageRecorder
from tests.conftest import bearer, create_run_and_step, login


async def test_every_attempt_lands_in_llm_calls(
    seeded_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, run_id, step_id = await create_run_and_step(session_factory)
    app = seeded_client.app  # type: ignore[attr-defined]
    llm = app.state.llm
    llm.provider.fail_next(LLMServerError("blip"))
    llm._retry = type(llm._retry)(max_attempts=3, base_delay=0.0, max_delay=0.0)

    response = await llm.complete(
        LLMRequest(system="s", messages=[LLMMessage("user", "hello")], task="plan"),
        CallContext(agent="planner", run_id=run_id, step_id=step_id),
    )
    async with session_factory() as s:
        rows = (
            await s.scalars(
                sa.select(LLMCall).where(LLMCall.run_id == run_id).order_by(LLMCall.created_at)
            )
        ).all()
    assert [r.status.value for r in rows] == ["FAILED", "SUCCEEDED"]
    assert rows[0].error and "LLMServerError" in rows[0].error
    ok = rows[1]
    assert ok.agent.value == "planner" and ok.provider == "mock" and ok.step_id == step_id
    assert ok.prompt_tokens == response.prompt_tokens > 0 and ok.cost_usd == Decimal(0)


async def test_recorder_never_raises(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from app.core.llm.client import UsageRecord

    recorder = DbUsageRecorder(session_factory)
    await recorder.record(UsageRecord(CallContext(agent="not-an-agent"), "mock", "m", "SUCCEEDED"))


async def test_tools_endpoint_lists_schemas_and_role_access(seeded_client: AsyncClient) -> None:
    assert (await seeded_client.get("/api/v1/tools")).status_code == 401
    viewer = await seeded_client.get(
        "/api/v1/tools", headers=bearer(await login(seeded_client, "viewer@demo.local"))
    )
    operator = await seeded_client.get(
        "/api/v1/tools", headers=bearer(await login(seeded_client, "operator@demo.local"))
    )
    assert viewer.status_code == operator.status_code == 200
    by_name = {t["name"]: t for t in operator.json()["items"]}
    assert set(by_name) == {"search_knowledge_base", "http_fetch", "calculator", "create_ticket",
                            "send_notification", "memory_read", "memory_write", "web_search",
                            "wikipedia_search", "arxiv_search"}  # fmt: skip
    notify = by_name["send_notification"]
    assert notify["risk_level"] == "HIGH" and notify["requires_approval"] is True
    assert "recipient" in notify["input_schema"]["properties"]
    assert all(t["allowed_for_you"] for t in by_name.values())
    viewer_tools = {t["name"]: t["allowed_for_you"] for t in viewer.json()["items"]}
    assert viewer_tools["search_knowledge_base"] is True and viewer_tools["create_ticket"] is False


async def test_readyz_reports_llm_and_trips_when_circuit_opens(client: AsyncClient) -> None:
    ready = await client.get("/readyz")
    assert ready.json()["checks"] == {"database": True, "llm": True}
    breaker = client.app.state.llm.breaker  # type: ignore[attr-defined]
    breaker._state = type(breaker.state).OPEN
    breaker._opened_at = breaker._clock()
    down = await client.get("/readyz")
    assert down.status_code == 503 and down.json()["checks"]["llm"] is False
