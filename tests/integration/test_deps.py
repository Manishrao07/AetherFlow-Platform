"""The per-request session dependency commits on success and rolls back on error."""

import pytest
import sqlalchemy as sa
from httpx import AsyncClient

from app.api.deps import get_session
from app.models import User


class _Request:
    def __init__(self, app: object) -> None:
        self.app = app


async def test_get_session_commits_on_success(engine: object, client: AsyncClient) -> None:
    gen = get_session(_Request(client.app))  # type: ignore[arg-type]
    session = await anext(gen)
    session.add(User(email="dep@test.dev", full_name="D", password_hash="x"))
    with pytest.raises(StopAsyncIteration):
        await anext(gen)  # finishing the generator commits
    async with client.app.state.session_factory() as check:  # type: ignore[attr-defined]
        assert await check.scalar(sa.select(sa.func.count()).select_from(User)) == 1


async def test_get_session_rolls_back_on_error(engine: object, client: AsyncClient) -> None:
    gen = get_session(_Request(client.app))  # type: ignore[arg-type]
    session = await anext(gen)
    session.add(User(email="rb@test.dev", full_name="D", password_hash="x"))
    await session.flush()
    with pytest.raises(RuntimeError):
        await gen.athrow(RuntimeError("boom"))
    async with client.app.state.session_factory() as check:  # type: ignore[attr-defined]
        assert await check.scalar(sa.select(sa.func.count()).select_from(User)) == 0
