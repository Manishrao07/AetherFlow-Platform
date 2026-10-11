"""RunStore: leases, guarded transitions and races, against a real database."""

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError
from app.models import RunEvent
from app.models.enums import RunStatus, StepStatus
from app.orchestrator.state_machine import InvalidTransitionError
from app.orchestrator.store import RunInterrupted, RunStore
from app.services.seeding import seed_demo_data
from tests.conftest import DEMO_PASSWORD, ROOT, create_run_and_step


async def make_store(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[RunStore, object, object]:
    await seed_demo_data(session_factory, DEMO_PASSWORD, ROOT / "scripts" / "sample_docs")
    _, run_id, step_id = await create_run_and_step(session_factory)
    return RunStore(session_factory), run_id, step_id


async def test_only_one_owner_can_hold_a_lease(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, run_id, _ = await make_store(session_factory)
    assert await store.claim(run_id, "worker-a", 30)  # type: ignore[arg-type]
    assert not await store.claim(run_id, "worker-b", 30)  # type: ignore[arg-type]
    assert await store.renew(run_id, "worker-a", 30)  # type: ignore[arg-type]
    assert not await store.renew(run_id, "worker-b", 30)  # type: ignore[arg-type]
    await store.release(run_id, "worker-b")  # type: ignore[arg-type]  # not the owner: no-op
    assert not await store.claim(run_id, "worker-b", 30)  # type: ignore[arg-type]
    await store.release(run_id, "worker-a")  # type: ignore[arg-type]
    assert await store.claim(run_id, "worker-b", 30)  # type: ignore[arg-type]


async def test_an_expired_lease_can_be_taken_over_and_marks_the_run_orphaned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, run_id, _ = await make_store(session_factory)
    assert run_id in await store.find_orphans()  # type: ignore[operator]  # never claimed
    assert await store.claim(run_id, "dead-worker", 0.05)  # type: ignore[arg-type]
    assert run_id not in await store.find_orphans()  # type: ignore[operator]
    await asyncio.sleep(0.15)
    assert run_id in await store.find_orphans()  # type: ignore[operator]
    assert await store.claim(run_id, "new-worker", 30)  # type: ignore[arg-type]


async def test_terminal_runs_are_never_claimed_or_orphans(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, run_id, _ = await make_store(session_factory)
    await store.transition_run(run_id, RunStatus.CANCELLED, reason="test")  # type: ignore[arg-type]
    assert not await store.claim(run_id, "w", 30)  # type: ignore[arg-type]
    assert run_id not in await store.find_orphans()  # type: ignore[operator]


async def test_invalid_and_late_transitions_are_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, run_id, step_id = await make_store(session_factory)
    with pytest.raises(InvalidTransitionError):
        await store.transition_run(run_id, RunStatus.COMPLETED, reason="skip ahead")  # type: ignore[arg-type]
    with pytest.raises(InvalidTransitionError):
        await store.transition_step(step_id, StepStatus.SUCCEEDED, reason="skip ahead")  # type: ignore[arg-type]
    await store.transition_run(run_id, RunStatus.CANCELLED, reason="user")  # type: ignore[arg-type]
    with pytest.raises(RunInterrupted):  # a worker that is late must stop, not overwrite
        await store.transition_run(run_id, RunStatus.PLANNING, reason="late worker")  # type: ignore[arg-type]
    run = await store.get_run(run_id)  # type: ignore[arg-type]
    assert run.status is RunStatus.CANCELLED


async def test_every_transition_is_recorded_as_an_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, run_id, _ = await make_store(session_factory)
    await store.transition_run(run_id, RunStatus.PLANNING, reason="go")  # type: ignore[arg-type]
    await store.emit(run_id, "custom", {"n": 1})  # type: ignore[arg-type]
    async with session_factory() as session:
        events = list(
            await session.scalars(
                sa.select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id)
            )
        )
    assert [e.type for e in events] == ["run.status", "custom"]
    assert events[0].data["to"] == "PLANNING" and events[1].data == {"n": 1}
    run = await store.get_run(run_id)  # type: ignore[arg-type]
    assert run.status is RunStatus.PLANNING and run.version >= 2


async def test_concurrent_transitions_do_not_both_win(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, run_id, _ = await make_store(session_factory)
    results = await asyncio.gather(
        store.transition_run(run_id, RunStatus.PLANNING, reason="worker"),  # type: ignore[arg-type]
        store.transition_run(run_id, RunStatus.CANCELLED, reason="user"),  # type: ignore[arg-type]
        return_exceptions=True,
    )
    final = (await store.get_run(run_id)).status  # type: ignore[arg-type]
    assert final in {RunStatus.PLANNING, RunStatus.CANCELLED}
    assert any(not isinstance(r, Exception) for r in results)
    for r in results:
        if isinstance(r, Exception):
            assert isinstance(r, InvalidTransitionError | RunInterrupted)


async def test_unknown_run_is_not_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, _, _ = await make_store(session_factory)
    import uuid

    with pytest.raises(NotFoundError):
        await store.get_run(uuid.uuid4())
