"""Async engine / session factory, startup connection retry and a readiness probe.

What: builds the pooled async engine, a session factory, ``wait_for_db`` (retry loop used at
boot) and ``check_db`` (fast probe used by ``/readyz``).
Why: in Docker the API may start before Postgres accepts connections; retrying with a
delay is far more robust than crashing and relying on the orchestrator to restart us.
Connects to: ``app/main.py`` lifespan, the health router, scripts and tests.
"""

from __future__ import annotations

import asyncio
import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings

logger = logging.getLogger(__name__)


def build_engine(settings: Settings, *, null_pool: bool = False) -> AsyncEngine:
    """Create the async engine.

    WHY pool_pre_ping: transparently replaces connections that Postgres closed while idle.
    WHY null_pool option: tests create many short-lived event loops; pooled asyncpg
    connections cannot be shared across loops.
    """
    if null_pool:
        return create_async_engine(settings.database_url, poolclass=NullPool)
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory; expire_on_commit=False so objects stay usable after commit."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def wait_for_db(engine: AsyncEngine, retries: int, delay_seconds: float) -> None:
    """Block until a ``SELECT 1`` succeeds, or raise after ``retries`` attempts."""
    for attempt in range(1, retries + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
            logger.info("database reachable", extra={"attempt": attempt})
            return
        except Exception as exc:
            logger.warning(
                "database not ready, retrying",
                extra={"attempt": attempt, "max_attempts": retries, "error": type(exc).__name__},
            )
            await asyncio.sleep(delay_seconds)
    raise RuntimeError(f"database not reachable after {retries} attempts")


async def check_db(engine: AsyncEngine, timeout_seconds: float = 2.0) -> bool:
    """Readiness probe: True if the DB answers quickly. Never raises."""
    try:
        async with asyncio.timeout(timeout_seconds):
            async with engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
        return True
    except Exception:
        logger.warning("database readiness check failed", exc_info=True)
        return False
