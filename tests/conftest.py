"""Shared pytest fixtures.

Strategy: unit tests need nothing external. Integration tests run against a REAL Postgres
(the schema relies on Postgres features - tsvector, triggers, partial indexes - that SQLite
cannot emulate). The test database is created on demand and migrated with Alembic, so the
tests also prove the migration works.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
import sqlalchemy as sa
import uvicorn
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://wf:wf@localhost:5432/wf_test"
)

# Must be set BEFORE app modules read settings.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-1234")
os.environ["ENVIRONMENT"] = "test"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["LOG_JSON"] = "false"

from app.agents import AgentSet, build_agents  # noqa: E402
from app.agents.base import AgentDeps  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.db.session import build_engine, build_session_factory  # noqa: E402
from app.core.llm.client import build_llm_client  # noqa: E402
from app.core.llm.mock_provider import MockProvider  # noqa: E402
from app.core.rbac import Principal  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.seeding import seed_demo_data  # noqa: E402
from app.tools.builtin import build_tool_registry  # noqa: E402

DEMO_PASSWORD = "Demo#Pass123"


def make_settings(**overrides: object) -> Settings:
    """Build Settings from explicit values only (ignores any .env file)."""
    values: dict[str, object] = {
        "database_url": TEST_DATABASE_URL,
        "jwt_secret": "test-secret-test-secret-test-secret-1234",
        "environment": "test",
        "log_json": False,
        "db_connect_retries": 3,
        "db_connect_retry_delay_seconds": 0.1,
        # WHY off: most tests hammer /auth/login; limiter behaviour has its own tests.
        "rate_limit_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def make_principal(*roles: str, email: str = "tester@demo.local") -> Principal:
    """A caller with the given roles (no database row needed for unit tests)."""
    return Principal(uuid.uuid4(), email, frozenset(roles or ("operator",)))


def make_agents(**setting_overrides: object) -> tuple[AgentSet, MockProvider]:
    """Agents wired to the mock LLM and the real registry; NO database.

    For tests of agents that only talk to the LLM (planner, review). The executor and session
    factory are None, so using them raises immediately instead of silently touching a database.
    """
    settings = make_settings(**setting_overrides)
    llm = build_llm_client(settings)
    deps = AgentDeps(
        llm=llm,
        registry=build_tool_registry(settings),
        executor=None,  # type: ignore[arg-type]
        session_factory=None,  # type: ignore[arg-type]
        settings=settings,
    )
    agents = build_agents(deps)
    provider = llm.provider
    assert isinstance(provider, MockProvider)
    return agents, provider


async def _ensure_database_exists(url: str) -> None:
    """Create the test database if missing (connects to the maintenance DB 'postgres')."""
    parsed = make_url(url)
    conn = await asyncpg.connect(
        host=parsed.host,
        port=parsed.port,
        user=parsed.username,
        password=parsed.password,
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", parsed.database
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[None]:
    """Create the test DB and run `alembic upgrade head` once per test session."""
    asyncio.run(_ensure_database_exists(TEST_DATABASE_URL))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test with all non-reference tables emptied first."""
    engine = build_engine(make_settings(), null_pool=True)
    async with engine.begin() as conn:
        # WHY CASCADE from users/conversations/documents: clears every dependent table.
        # roles (reference data) and audit_logs (append-only, truncate is blocked) are kept.
        await conn.execute(sa.text("TRUNCATE users, conversations, documents CASCADE"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_session_factory(engine)


@pytest_asyncio.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@asynccontextmanager
async def running_app(**setting_overrides: object) -> AsyncIterator[AsyncClient]:
    """Create an app with the given settings, start its lifespan and yield a client."""
    app = create_app(make_settings(**setting_overrides))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        http.app = app  # type: ignore[attr-defined]
        yield http


@pytest_asyncio.fixture
async def client(migrated_database: None) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to a real app instance (lifespan started manually)."""
    async with running_app() as http:
        yield http


@pytest_asyncio.fixture
async def seeded_client(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AsyncClient]:
    """Client against a freshly truncated DB containing the four demo users."""
    await seed_demo_data(session_factory, DEMO_PASSWORD, ROOT / "scripts" / "sample_docs")
    async with running_app() as http:
        yield http


async def login(http: AsyncClient, email: str, password: str = DEMO_PASSWORD) -> dict[str, str]:
    """Log in and return the token pair."""
    response = await http.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    tokens: dict[str, str] = response.json()
    return tokens


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def create_run_and_step(
    session_factory: async_sessionmaker[AsyncSession], user_email: str = "operator@demo.local"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert a workflow run + step for ``user_email``; returns (user_id, run_id, step_id)."""
    from app.models import User, WorkflowRun, WorkflowStep

    async with session_factory() as session, session.begin():
        user = await session.scalar(sa.select(User).where(User.email == user_email))
        assert user is not None
        run = WorkflowRun(user_id=user.id, goal="test goal")
        session.add(run)
        await session.flush()
        step = WorkflowStep(run_id=run.id, step_key="s1", position=1, agent="execution")
        session.add(step)
        await session.flush()
        return user.id, run.id, step.id


async def _reset_and_seed() -> None:
    engine = build_engine(make_settings(), null_pool=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text("TRUNCATE users, conversations, documents CASCADE"))
        await seed_demo_data(
            build_session_factory(engine), DEMO_PASSWORD, ROOT / "scripts" / "sample_docs"
        )
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def live_api_url(migrated_database: None) -> Iterator[str]:
    """A REAL API server (uvicorn on a free port, seeded demo users, worker on) for sync clients.

    WHY a thread: uvicorn needs its own event loop, while the code under test (the Streamlit UI
    client, the demo script) is synchronous and talks to it over a real socket.
    """
    asyncio.run(_reset_and_seed())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(make_settings(sse_poll_interval_seconds=0.05)),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "API did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
