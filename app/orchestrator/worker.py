"""Background execution: a swappable job queue plus an in-process asyncio worker.

What: ``JobQueue`` is the tiny interface the rest of the app uses ("please drive this run");
``InProcessWorker`` implements it with an asyncio queue and N consumer tasks.
Why this is safe to be so simple: the queue only carries a RUN ID as a wake-up signal. All real
state is in Postgres (status, steps, leases). If the process dies, nothing is lost: on the next
start (and every ``sweep_interval`` seconds) the worker asks the database for "active runs
whose lease expired" and drives them again. So the queue is a latency optimisation, not a
source of truth.
Swapping for Celery/Arq: implement ``JobQueue.enqueue`` by publishing the id to the broker and
have a worker process call ``WorkflowEngine.drive(run_id)``. Nothing else changes (see
docs/tradeoffs.md). ``app.orchestrator.runner`` already runs workers without the API.
Backpressure: ``max_concurrent_runs`` consumers means at most that many runs execute at once
per process; further runs wait in the queue.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.orchestrator.approvals import expire_overdue
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.store import RunStore

logger = logging.getLogger(__name__)


class JobQueue(Protocol):
    async def enqueue(self, run_id: uuid.UUID) -> None:
        """Ask a worker to drive ``run_id`` soon. Must be safe to call repeatedly."""


class NullQueue:
    """Used when this process runs no worker; separate worker processes find runs via sweeps."""

    async def enqueue(self, run_id: uuid.UUID) -> None:
        return None


class InProcessWorker:
    def __init__(
        self,
        engine: WorkflowEngine,
        store: RunStore,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._engine = engine
        self._store = store
        self._sf = session_factory
        self._settings = settings
        self._queue: asyncio.Queue[uuid.UUID] = asyncio.Queue()
        self._queued: set[uuid.UUID] = set()
        self._active: set[uuid.UUID] = set()
        self._rerun: set[uuid.UUID] = set()
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._consume(), name=f"run-consumer-{i}")
            for i in range(self._settings.max_concurrent_runs)
        ]
        self._tasks.append(asyncio.create_task(self._sweep_loop(), name="sweeper"))
        await self.sweep()  # recover whatever a previous process left behind
        logger.info("worker started", extra={"consumers": self._settings.max_concurrent_runs})

    async def stop(self) -> None:
        """Graceful shutdown: cancel consumers; each run releases its lease as it unwinds."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("worker stopped")

    # ---------------------------------------------------------------- queue
    async def enqueue(self, run_id: uuid.UUID) -> None:
        if run_id in self._active:
            self._rerun.add(run_id)  # being driven right now; drive it again when it yields
            return
        if run_id in self._queued:
            return
        self._queued.add(run_id)
        await self._queue.put(run_id)

    async def wait_idle(self, timeout: float = 30.0) -> None:
        """Block until nothing is queued or running (used by tests and graceful drains)."""

        async def idle() -> None:
            while self._queued or self._active or not self._queue.empty():
                await asyncio.sleep(0.02)

        await asyncio.wait_for(idle(), timeout)

    async def _consume(self) -> None:
        while True:
            run_id = await self._queue.get()
            self._queued.discard(run_id)
            self._active.add(run_id)
            try:
                await self._engine.drive(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("unexpected worker error", extra={"run_id": str(run_id)})
            finally:
                self._active.discard(run_id)
                self._queue.task_done()
                if run_id in self._rerun:
                    self._rerun.discard(run_id)
                    await self.enqueue(run_id)

    # --------------------------------------------------------------- sweeper
    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.sweep_interval_seconds)
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sweep failed")

    async def sweep(self) -> int:
        """Expire stale approvals, then re-queue runs that lost their worker."""
        await expire_overdue(self._sf, self._store)
        recovered = 0
        for run_id in await self._engine.recover():
            if run_id not in self._active and run_id not in self._queued:
                await self.enqueue(run_id)
                recovered += 1
        if recovered:
            logger.warning("re-queued orphaned runs", extra={"count": recovered})
        return recovered
