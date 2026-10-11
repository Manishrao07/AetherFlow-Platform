"""Tools that touch the database, run through the real executor (idempotency, audit, redaction)."""

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit import AuditService
from app.core.rbac import Principal
from app.models import NotificationOutbox, Ticket, ToolExecution, User
from app.services.seeding import seed_demo_data
from app.tools.base import ApprovalRequiredError, ToolContext, ToolError, ToolNotAllowedError
from app.tools.builtin import build_tool_registry
from app.tools.executor import ToolExecutor
from tests.conftest import DEMO_PASSWORD, ROOT, create_run_and_step, make_settings

ALL = ["search_knowledge_base", "http_fetch", "calculator", "create_ticket", "send_notification",
       "memory_read", "memory_write", "web_search", "wikipedia_search", "arxiv_search"]  # fmt: skip


@pytest.fixture
async def env(session_factory: async_sessionmaker[AsyncSession]):
    await seed_demo_data(session_factory, DEMO_PASSWORD, ROOT / "scripts" / "sample_docs")
    registry = build_tool_registry(make_settings())
    executor = ToolExecutor(registry, session_factory, AuditService(session_factory))
    return registry, executor, session_factory


async def context(
    session_factory: async_sessionmaker[AsyncSession],
    email: str = "operator@demo.local",
    *,
    approved: bool = False,
    step: tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None = None,
) -> ToolContext:
    user_id, run_id, step_id = step or await create_run_and_step(session_factory, email)
    async with session_factory() as s:
        user = await s.get(User, user_id)
        assert user is not None
        principal = Principal(user.id, user.email, frozenset(r.name for r in user.roles))
    return ToolContext(
        principal=principal, session_factory=session_factory, settings=make_settings(),
        run_id=run_id, step_id=step_id, side_effect_key=str(step_id), approved=approved,
    )  # fmt: skip


async def run(
    executor: ToolExecutor, name: str, inputs: dict[str, Any], ctx: ToolContext, attempt: int = 1
):
    return await executor.execute(
        name, inputs, ctx, attempt=attempt, plan_tools=ALL, agent_allowlist=ALL
    )


async def count(session_factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with session_factory() as s:
        return int(await s.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


async def test_calculator_persists_execution_row(env) -> None:
    _, executor, sf = env
    ctx = await context(sf)
    result = await run(executor, "calculator", {"expression": "2+2*5"}, ctx)
    assert result.output["result"] == 12 and not result.replayed
    async with sf() as s:
        row = await s.scalar(sa.select(ToolExecution))
    assert row is not None and row.status.value == "SUCCEEDED" and row.attempt == 1
    assert row.idempotency_key == f"{ctx.step_id}:calculator:1" and row.output["result"] == 12


async def test_same_attempt_is_replayed_not_rerun(env) -> None:
    _, executor, sf = env
    ctx = await context(sf)
    first = await run(executor, "create_ticket", {"title": "Disk full"}, ctx)
    again = await run(executor, "create_ticket", {"title": "Disk full"}, ctx)
    assert again.replayed and again.output == first.output
    assert await count(sf, ToolExecution) == 1 and await count(sf, Ticket) == 1


async def test_retry_of_same_step_cannot_create_duplicate_ticket(env) -> None:
    """Attempt 2 is a NEW execution record but must reuse the step-level side-effect key."""
    _, executor, sf = env
    ctx = await context(sf)
    first = await run(
        executor, "create_ticket", {"title": "DB down", "severity": "SEV1"}, ctx, attempt=1
    )
    second = await run(
        executor, "create_ticket", {"title": "DB down", "severity": "SEV1"}, ctx, attempt=2
    )
    assert first.output["created"] is True and second.output["created"] is False
    assert first.output["ticket_id"] == second.output["ticket_id"]
    assert await count(sf, Ticket) == 1 and await count(sf, ToolExecution) == 2 - 0 * 1


async def test_send_notification_requires_approval_then_runs_once(env) -> None:
    _, executor, sf = env
    step = await create_run_and_step(sf)
    unapproved = await context(sf, step=step)
    body = {"recipient": "boss@example.com", "subject": "Report", "body": "Done."}
    with pytest.raises(ApprovalRequiredError):
        await run(executor, "send_notification", body, unapproved)
    assert await count(sf, NotificationOutbox) == 0

    approved = await context(sf, approved=True, step=step)
    result = await run(executor, "send_notification", body, approved)
    assert result.output["status"] == "sent (mock)" and result.output["created"] is True
    await run(executor, "send_notification", body, approved, attempt=2)  # retry: no second email
    assert await count(sf, NotificationOutbox) == 1

    async with sf() as s:
        entry = await s.scalar(
            sa.text(
                "SELECT details FROM audit_logs WHERE action='tool.executed' "
                "AND resource_id='send_notification' ORDER BY created_at DESC LIMIT 1"
            )
        )
    assert entry["risk"] == "HIGH" and entry["approved"] is True


async def test_notification_rejects_bad_recipient(env) -> None:
    _, executor, sf = env
    ctx = await context(sf, approved=True)
    with pytest.raises(ToolError):
        await run(
            executor,
            "send_notification",
            {"recipient": "not-an-email", "subject": "s", "body": "b"},
            ctx,
        )


async def test_viewer_is_blocked_and_block_is_audited(env) -> None:
    _, executor, sf = env
    ctx = await context(sf, "viewer@demo.local")
    with pytest.raises(ToolNotAllowedError):
        await run(executor, "create_ticket", {"title": "x"}, ctx)
    async with sf() as s:
        blocked = await s.scalar(
            sa.text(
                "SELECT count(*) FROM audit_logs "
                "WHERE action='tool.blocked' AND actor_email='viewer@demo.local'"
            )
        )
    assert blocked >= 1 and await count(sf, ToolExecution) == 0


async def test_tool_outside_plan_is_blocked(env) -> None:
    _, executor, sf = env
    ctx = await context(sf)
    with pytest.raises(ToolNotAllowedError, match="plan"):
        await executor.execute(
            "create_ticket",
            {"title": "x"},
            ctx,
            attempt=1,
            plan_tools=["calculator"],
            agent_allowlist=ALL,
        )


async def test_failed_attempt_is_recorded_with_error(env) -> None:
    _, executor, sf = env
    ctx = await context(sf)
    with pytest.raises(ToolError, match="division by zero"):
        await run(executor, "calculator", {"expression": "1/0"}, ctx)
    async with sf() as s:
        row = await s.scalar(sa.select(ToolExecution))
    assert row is not None and row.status.value == "FAILED" and "division" in (row.error or "")
    # a later attempt with the same step is allowed to succeed (new key), failure never replays
    ok = await run(executor, "calculator", {"expression": "1/2"}, ctx, attempt=2)
    assert ok.output["result"] == 0.5 and not ok.replayed


async def test_stored_trace_is_redacted(env) -> None:
    _, executor, sf = env
    ctx = await context(sf)
    await run(
        executor, "memory_write", {"key": "contact", "content": "reach me at jane@corp.com"}, ctx
    )
    async with sf() as s:
        row = await s.scalar(sa.select(ToolExecution))
        memory = await s.scalar(sa.text("SELECT content FROM memories WHERE key='contact'"))
    assert row is not None and "jane@corp.com" not in str(row.input)
    assert (
        memory == "reach me at jane@corp.com"
    )  # the real data is intact; only the TRACE is masked


async def test_knowledge_search_returns_cited_passages(env) -> None:
    _, executor, sf = env
    ctx = await context(sf)
    result = await run(
        executor, "search_knowledge_base", {"query": "how long do refunds take?"}, ctx
    )
    top = result.output["results"][0]
    assert top["title"] == "Refund Policy" and top["citation"] == "Refund Policy #1"
    assert "refund" in top["content"].lower() and top["score"] > 0
    none = await run(
        executor, "search_knowledge_base", {"query": "zebra quantum submarine"}, ctx, attempt=2
    )
    assert none.output["results"] == []


async def test_memory_roundtrip_and_user_isolation(env) -> None:
    _, executor, sf = env
    alice = await context(sf, "operator@demo.local")
    bob = await context(sf, "approver@demo.local")
    await run(
        executor,
        "memory_write",
        {"key": "style", "content": "Prefers weekly summaries", "importance": 4},
        alice,
    )
    await run(
        executor,
        "memory_write",
        {"key": "style", "content": "Prefers weekly digests", "importance": 5},
        alice,
        attempt=2,
    )
    mine = await run(executor, "memory_read", {"query": "weekly"}, alice)
    assert [m["content"] for m in mine.output["memories"]] == [
        "Prefers weekly digests"
    ]  # upsert, not duplicate
    theirs = await run(executor, "memory_read", {"query": "weekly"}, bob)
    assert theirs.output["memories"] == []  # strictly private
    assert (await run(executor, "memory_read", {}, alice, attempt=3)).output["memories"][0][
        "key"
    ] == "style"


async def test_executor_requires_step(env) -> None:
    _, executor, sf = env
    ctx = await context(sf)
    no_step = ToolContext(ctx.principal, sf, ctx.settings)
    with pytest.raises(ValueError, match="step_id"):
        await run(executor, "calculator", {"expression": "1"}, no_step)
