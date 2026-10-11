"""Seeding creates one user per role + documents, and is idempotent."""

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import verify_password
from app.models import Document, DocumentChunk, User
from app.services.seeding import DEMO_USERS, seed_demo_data

DOCS = Path(__file__).resolve().parents[2] / "scripts" / "sample_docs"


async def test_seed_creates_users_and_documents(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await seed_demo_data(session_factory, "Pw#123456", DOCS)
    assert result.users_created == len(DEMO_USERS) == 4
    assert result.documents_created == 3

    async with session_factory() as s:
        users = (await s.scalars(sa.select(User))).all()
        assert {u.roles[0].name for u in users} == {"admin", "approver", "operator", "viewer"}
        assert all(verify_password("Pw#123456", u.password_hash) for u in users)
        assert await s.scalar(sa.select(sa.func.count()).select_from(DocumentChunk)) >= 3
        assert all(d.chunk_count >= 1 for d in (await s.scalars(sa.select(Document))).all())


async def test_seed_is_idempotent(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_demo_data(session_factory, "Pw#123456", DOCS)
    again = await seed_demo_data(session_factory, "Pw#123456", DOCS)
    assert (again.users_created, again.documents_created) == (0, 0)
    async with session_factory() as s:
        assert await s.scalar(sa.select(sa.func.count()).select_from(User)) == 4
