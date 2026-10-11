"""The stand-alone worker process entry point (python -m app.orchestrator.runner)."""

import asyncio
import os
import signal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.orchestrator import runner
from tests.conftest import make_settings
from tests.e2e.helpers import as_user, flow_app, start, wait_status


async def test_runner_serves_metrics_drives_runs_and_stops_on_sigterm(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    started_servers: list[int] = []

    def fake_server(port: int, registry: Any = None) -> None:
        started_servers.append(port)

    monkeypatch.setattr(runner, "get_settings", lambda: make_settings(worker_metrics_port=19464))
    monkeypatch.setattr(runner, "start_http_server", fake_server)

    # An API-only app creates the run; the runner process (below) must pick it up.
    async with flow_app(session_factory, worker_enabled=False) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, "What is the refund window for digital goods?")

        loop = asyncio.get_running_loop()
        loop.call_later(1.5, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(runner.run_worker(), 20)

        done = await wait_status(http, op, run["id"], "COMPLETED")
        assert done["review_verdict"] == "APPROVED"
    assert started_servers == [19464]
