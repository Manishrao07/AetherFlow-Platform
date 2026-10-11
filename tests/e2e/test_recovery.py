"""Crash recovery: a run whose worker died is picked up again and finishes exactly once."""

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.e2e.helpers import as_user, flow_app, start, step_by_key, timeline, wait_for, wait_status


async def test_run_is_recovered_after_its_worker_dies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, run_lease_seconds=1.0) as http:
        state = http.app.state  # type: ignore[attr-defined]
        op = await as_user(http, "operator")

        # First call hangs (the "crashed" worker); later calls are normal.
        tool = state.tool_registry.get("calculator")
        real = tool.run
        calls = {"n": 0}

        async def run(args: Any, ctx: Any) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(60)
            return await real(args, ctx)  # type: ignore[no-any-return]

        tool.run = run

        # A crashed process cannot release its lease, so make release a no-op.
        async def never_release(run_id: Any, owner: str) -> None:
            return None

        state.run_store.release = never_release

        created = await start(http, op, "Calculate 2 + 3 please")

        async def running() -> bool:
            tl = await timeline(http, op, created["id"])
            return any(s["step_key"] == "s2" and s["status"] == "RUNNING" for s in tl["steps"])

        await wait_for(running, what="step running")
        await state.worker.stop()  # the "crash"
        stuck = await timeline(http, op, created["id"])
        assert step_by_key(stuck, "s2")["status"] == "RUNNING"
        assert (await http.get(f"/api/v1/workflows/{created['id']}", headers=op)).json()[
            "status"
        ] == "EXECUTING"

        await asyncio.sleep(1.3)  # let the dead worker's lease expire
        await state.worker.start()  # a new process starts; startup sweep recovers the orphan
        await wait_status(http, op, created["id"], "COMPLETED")
        step = step_by_key(await timeline(http, op, created["id"]), "s2")
        assert step["status"] == "SUCCEEDED" and step["attempts"] == 1  # same attempt number reused
