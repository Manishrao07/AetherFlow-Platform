"""The migrated schema enforces the rules the design promises."""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app.models import (
    Approval,
    AuditLog,
    Conversation,
    Document,
    DocumentChunk,
    Memory,
    User,
    WorkflowRun,
    WorkflowStep,
)
from app.models.enums import ApprovalKind, RiskLevel, RunStatus

EXPECTED_TABLES = {
    "users",
    "roles",
    "user_roles",
    "conversations",
    "messages",
    "workflow_runs",
    "workflow_steps",
    "step_dependencies",
    "tool_executions",
    "llm_calls",
    "approvals",
    "documents",
    "document_chunks",
    "memories",
    "audit_logs",
}


async def _user(session: AsyncSession, email: str = "u@test.dev") -> User:
    user = User(email=email, full_name="T", password_hash="x")
    session.add(user)
    await session.flush()
    return user


async def _run(session: AsyncSession, user: User) -> WorkflowRun:
    run = WorkflowRun(user_id=user.id, goal="do it")
    session.add(run)
    await session.flush()
    return run


async def test_all_tables_exist(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
    assert names >= EXPECTED_TABLES


async def test_roles_are_seeded_by_migration(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (await conn.execute(sa.text("SELECT name FROM roles ORDER BY name"))).scalars().all()
    assert list(rows) == ["admin", "approver", "operator", "viewer"]


async def test_email_unique_case_insensitively(session: AsyncSession) -> None:
    await _user(session, "Bob@Test.dev")
    with pytest.raises(IntegrityError):
        await _user(session, "bob@test.dev")


async def test_invalid_run_status_rejected_by_check_constraint(session: AsyncSession) -> None:
    user = await _user(session)
    await session.execute(
        sa.text("INSERT INTO workflow_runs (user_id, goal) VALUES (:u, 'g')"), {"u": user.id}
    )
    with pytest.raises(IntegrityError, match="run_status"):
        await session.execute(
            sa.text("INSERT INTO workflow_runs (user_id, goal, status) VALUES (:u, 'g', 'BOGUS')"),
            {"u": user.id},
        )


async def test_run_defaults_and_optimistic_locking(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup, setup.begin():
        run = await _run(setup, await _user(setup))
        run_id = run.id
        assert run.status is RunStatus.CREATED

    async with session_factory() as a, session_factory() as b:
        run_a = await a.get(WorkflowRun, run_id)
        run_b = await b.get(WorkflowRun, run_id)
        assert run_a is not None and run_b is not None
        run_a.status = RunStatus.PLANNING
        await a.commit()
        run_b.status = RunStatus.CANCELLED
        with pytest.raises(StaleDataError):  # second writer loses instead of overwriting
            await b.commit()


async def test_approval_requester_cannot_decide_own_approval(session: AsyncSession) -> None:
    user = await _user(session)
    run = await _run(session, user)
    approval = Approval(
        run_id=run.id,
        kind=ApprovalKind.PLAN,
        risk_level=RiskLevel.HIGH,
        reason="r",
        requested_by=user.id,
        decided_by=user.id,
        expires_at=sa.func.now(),
        status="APPROVED",
        decided_at=sa.func.now(),
    )
    session.add(approval)
    with pytest.raises(IntegrityError, match="separation_of_duties"):
        await session.flush()


async def test_decision_requires_decider(session: AsyncSession) -> None:
    user = await _user(session)
    run = await _run(session, user)
    session.add(
        Approval(
            run_id=run.id,
            kind=ApprovalKind.STEP,
            risk_level=RiskLevel.HIGH,
            reason="r",
            requested_by=user.id,
            expires_at=sa.func.now(),
            status="APPROVED",
        )
    )
    with pytest.raises(IntegrityError, match="decision_complete"):
        await session.flush()


async def test_cascade_delete_removes_dependents(session: AsyncSession) -> None:
    user = await _user(session)
    run = await _run(session, user)
    session.add(WorkflowStep(run_id=run.id, step_key="s1", position=1, agent="execution"))
    await session.flush()
    await session.delete(user)
    await session.flush()
    count = await session.scalar(sa.select(sa.func.count()).select_from(WorkflowStep))
    assert count == 0


async def test_step_key_unique_per_run(session: AsyncSession) -> None:
    run = await _run(session, await _user(session))
    session.add_all(
        [WorkflowStep(run_id=run.id, step_key="s1", position=i, agent="execution") for i in (1, 2)]
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_self_dependency_rejected(session: AsyncSession) -> None:
    run = await _run(session, await _user(session))
    step = WorkflowStep(run_id=run.id, step_key="s1", position=1, agent="execution")
    session.add(step)
    await session.flush()
    with pytest.raises(IntegrityError, match="no_self_dependency"):
        await session.execute(
            sa.text("INSERT INTO step_dependencies VALUES (:s, :s)"), {"s": step.id}
        )


async def test_full_text_search_vector_is_generated(session: AsyncSession) -> None:
    user = await _user(session)
    doc = Document(owner_id=user.id, title="D", content_sha256="a" * 64, chunk_count=1)
    session.add(doc)
    await session.flush()
    session.add(
        DocumentChunk(document_id=doc.id, chunk_index=0, content="Refunds are issued within days")
    )
    session.add(Memory(user_id=user.id, key="style", content="prefers weekly summaries"))
    await session.flush()
    hits = await session.scalar(
        sa.select(sa.func.count())
        .select_from(DocumentChunk)
        .where(
            DocumentChunk.search_vector.op("@@")(sa.func.websearch_to_tsquery("english", "refund"))
        )
    )
    memory_hits = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Memory)
        .where(Memory.search_vector.op("@@")(sa.func.websearch_to_tsquery("english", "summary")))
    )
    assert hits == 1 and memory_hits == 1


async def test_memory_key_unique_per_user(session: AsyncSession) -> None:
    user = await _user(session)
    session.add_all(
        [
            Memory(user_id=user.id, key="k", content="a"),
            Memory(user_id=user.id, key="k", content="b"),
        ]
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_conversation_defaults(session: AsyncSession) -> None:
    user = await _user(session)
    conv = Conversation(user_id=user.id, title="Chat")
    session.add(conv)
    await session.flush()
    await session.refresh(conv)
    assert conv.status.value == "ACTIVE" and conv.summary == ""


async def test_audit_log_is_append_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s, s.begin():
        entry = AuditLog(action="login", actor_email="a@b.co", details={"ip": "1.2.3.4"})
        s.add(entry)
        await s.flush()
        entry_id = entry.id

    for statement in (
        "UPDATE audit_logs SET action = 'x' WHERE id = :id",
        "DELETE FROM audit_logs WHERE id = :id",
    ):
        async with session_factory() as s:
            with pytest.raises(DBAPIError, match="append-only"):
                await s.execute(sa.text(statement), {"id": entry_id})

    async with session_factory() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(sa.text("TRUNCATE audit_logs"))

    async with session_factory() as s:
        assert await s.get(AuditLog, entry_id) is not None


async def test_audit_log_survives_user_deletion(session: AsyncSession) -> None:
    user = await _user(session)
    session.add(AuditLog(actor_id=user.id, actor_email=user.email, action="login"))
    await session.flush()
    await session.delete(user)
    await session.flush()  # no FK, so deletion neither fails nor touches the audit row
    assert (
        await session.scalar(
            sa.select(sa.func.count()).select_from(AuditLog).where(AuditLog.actor_id == user.id)
        )
        == 1
    )


def test_uuid_helper_is_unique() -> None:
    assert uuid.uuid4() != uuid.uuid4()
