"""Helpers for end-to-end workflow tests (real app, real Postgres, mock LLM, real worker)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.seeding import seed_demo_data
from app.tools.base import Tool, ToolError
from tests.conftest import DEMO_PASSWORD, ROOT, bearer, login, running_app

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


@asynccontextmanager
async def flow_app(
    session_factory: async_sessionmaker[AsyncSession], **overrides: object
) -> AsyncIterator[AsyncClient]:
    """A running app with seeded demo users, instant retries and a fast SSE poll."""
    await seed_demo_data(session_factory, DEMO_PASSWORD, ROOT / "scripts" / "sample_docs")
    settings: dict[str, object] = {
        "step_retry_base_delay_seconds": 0.0,
        "step_retry_max_delay_seconds": 0.0,
        "llm_retry_base_delay_seconds": 0.0,
        "sse_poll_interval_seconds": 0.05,
        "sweep_interval_seconds": 3600.0,  # sweeps are triggered explicitly by tests
    }
    settings.update(overrides)
    async with running_app(**settings) as http:
        yield http


async def as_user(http: AsyncClient, name: str) -> dict[str, str]:
    return bearer(await login(http, f"{name}@demo.local"))


async def start(
    http: AsyncClient, headers: dict[str, str], goal: str, **extra: Any
) -> dict[str, Any]:
    response = await http.post("/api/v1/workflows", json={"goal": goal, **extra}, headers=headers)
    assert response.status_code in {200, 202}, response.text
    body: dict[str, Any] = response.json()
    return body


async def get_run(http: AsyncClient, headers: dict[str, str], run_id: str) -> dict[str, Any]:
    response = await http.get(f"/api/v1/workflows/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def wait_status(
    http: AsyncClient,
    headers: dict[str, str],
    run_id: str,
    *targets: str,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Poll until the run reaches one of ``targets`` (fail loudly with the last state)."""
    deadline = time.monotonic() + timeout
    run: dict[str, Any] = {}
    while time.monotonic() < deadline:
        run = await get_run(http, headers, run_id)
        if run["status"] in targets:
            return run
        if run["status"] in TERMINAL and run["status"] not in targets:
            break
        await asyncio.sleep(0.03)
    raise AssertionError(
        f"run {run_id} never reached {targets}; last: {run.get('status')} {run.get('error')}"
    )


async def timeline(http: AsyncClient, headers: dict[str, str], run_id: str) -> dict[str, Any]:
    response = await http.get(f"/api/v1/workflows/{run_id}/timeline", headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def wait_for(
    predicate: Callable[[], Awaitable[bool]], timeout: float = 10.0, what: str = "condition"
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.03)
    raise AssertionError(f"timed out waiting for {what}")


def status_path(tl: dict[str, Any]) -> list[str]:
    """The run's status history, from its persisted events."""
    return [e["data"]["to"] for e in tl["events"] if e["type"] == "run.status"]


def step_by_key(tl: dict[str, Any], key: str, revision: int | None = None) -> dict[str, Any]:
    matches = [
        s
        for s in tl["steps"]
        if s["step_key"] == key and (revision is None or s["revision"] == revision)
    ]
    assert matches, f"no step {key!r}"
    return matches[-1]


def make_flaky(
    tool: Tool,
    fail_times: int,
    error: Callable[[], Exception] = lambda: ToolError("transient glitch", retryable=True),
) -> dict[str, int]:
    """Make ``tool.run`` fail its first ``fail_times`` calls (instance-level patch)."""
    real = tool.run
    calls = {"n": 0}

    async def run(args: Any, ctx: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise error()
        return await real(args, ctx)

    tool.run = run  # type: ignore[method-assign]
    return calls


async def read_sse(
    http: AsyncClient, headers: dict[str, str], run_id: str, **extra_headers: str
) -> list[dict[str, str]]:
    """Consume the whole event stream (ends when the run is terminal) into a list of events."""
    events: list[dict[str, str]] = []
    async with http.stream(
        "GET", f"/api/v1/workflows/{run_id}/events", headers={**headers, **extra_headers}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = (await response.aread()).decode()
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if fields:
            events.append(fields)
    return events


def new_id() -> str:
    return str(uuid.uuid4())


def parse_data(event: dict[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(event["data"])
    return data
