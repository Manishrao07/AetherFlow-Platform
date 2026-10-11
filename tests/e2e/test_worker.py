"""Worker mechanics: dedupe, re-run requests, error isolation, the sweeper, API-only mode."""

import asyncio
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.orchestrator.store import RunStore
from app.orchestrator.worker import InProcessWorker, NullQueue
from tests.conftest import make_settings
from tests.e2e.helpers import as_user, flow_app, start, wait_status


class StubEngine:
    """Stands in for WorkflowEngine: records drives, can block or fail on demand."""

    def __init__(self) -> None:
        self.driven: list[uuid.UUID] = []
        self.orphans: list[uuid.UUID] = []
        self.gate = asyncio.Event()
        self.block = False
        self.fail_for: set[uuid.UUID] = set()
        self.recover_error = False

    async def drive(self, run_id: uuid.UUID) -> bool:
        self.driven.append(run_id)
        if run_id in self.fail_for:
            raise RuntimeError("engine blew up")
        if self.block:
            await self.gate.wait()
        return True

    async def recover(self) -> list[uuid.UUID]:
        if self.recover_error:
            raise RuntimeError("database hiccup")
        found, self.orphans = self.orphans, []
        return found


def make_worker(
    session_factory: async_sessionmaker[AsyncSession], **settings: Any
) -> tuple[InProcessWorker, StubEngine]:
    engine = StubEngine()
    worker = InProcessWorker(
        engine,  # type: ignore[arg-type]
        RunStore(session_factory),
        session_factory,
        make_settings(max_concurrent_runs=2, **settings),
    )
    return worker, engine


async def test_duplicate_enqueues_drive_a_run_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker, engine = make_worker(session_factory, sweep_interval_seconds=3600)
    run_id = uuid.uuid4()
    await worker.enqueue(run_id)
    await worker.enqueue(run_id)  # already queued -> ignored
    await worker.start()
    await worker.wait_idle(5)
    await worker.stop()
    assert engine.driven == [run_id]


async def test_enqueue_while_running_requests_one_more_pass(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker, engine = make_worker(session_factory, sweep_interval_seconds=3600)
    engine.block = True
    run_id = uuid.uuid4()
    await worker.start()
    await worker.enqueue(run_id)
    for _ in range(100):
        if engine.driven:
            break
        await asyncio.sleep(0.01)
    await worker.enqueue(run_id)  # new work arrived (e.g. approval) while the run is active
    await worker.enqueue(run_id)  # ...asking twice still means one extra pass
    engine.block = False
    engine.gate.set()
    await worker.wait_idle(5)
    await worker.stop()
    assert engine.driven == [run_id, run_id]


async def test_one_failing_run_does_not_stop_the_worker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker, engine = make_worker(session_factory, sweep_interval_seconds=3600)
    bad, good = uuid.uuid4(), uuid.uuid4()
    engine.fail_for.add(bad)
    await worker.start()
    await worker.enqueue(bad)
    await worker.enqueue(good)
    await worker.wait_idle(5)
    await worker.stop()
    assert engine.driven == [bad, good]


async def test_the_sweeper_requeues_orphans_and_survives_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker, engine = make_worker(session_factory, sweep_interval_seconds=0.05)
    await worker.start()
    engine.recover_error = True
    await asyncio.sleep(0.2)  # several failing sweeps; the loop must keep going
    engine.recover_error = False
    orphan = uuid.uuid4()
    engine.orphans = [orphan]
    for _ in range(200):
        if orphan in engine.driven:
            break
        await asyncio.sleep(0.01)
    await worker.stop()
    assert orphan in engine.driven


async def test_sweep_reports_how_many_runs_it_requeued(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker, engine = make_worker(session_factory, sweep_interval_seconds=3600)
    engine.orphans = [uuid.uuid4(), uuid.uuid4()]
    assert await worker.sweep() == 2
    assert await worker.sweep() == 0


async def test_null_queue_ignores_work() -> None:
    assert await NullQueue().enqueue(uuid.uuid4()) is None


async def test_api_only_mode_leaves_runs_for_a_separate_worker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, worker_enabled=False) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, "What is the refund window for digital goods?")
        await asyncio.sleep(0.5)
        waiting = await http.get(f"/api/v1/workflows/{run['id']}", headers=op)
        assert waiting.json()["status"] == "CREATED"  # nobody is driving it
        # a worker process (here: the in-process one, started late) finds it via its start-up sweep
        await http.app.state.worker.start()  # type: ignore[attr-defined]
        await wait_status(http, op, run["id"], "COMPLETED")
