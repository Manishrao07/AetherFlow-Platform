"""Retrieval cache: hits skip the database, ingest/delete invalidate, corrupt entries recover."""

import json

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import InMemoryCache
from app.core.observability.metrics import REGISTRY
from app.services.documents import ingest_document
from app.services.knowledge import KnowledgeSearch
from tests.conftest import bearer, login

BASE = "/api/v1/documents"
TRAVEL = "Hotel stays are capped at 200 USD per night unless a manager approves an exception."


def retrieval(result: str) -> float:
    labels = {"cache": "retrieval", "result": result}
    return REGISTRY.get_sample_value("cache_operations_total", labels) or 0.0


async def owner_id(session: AsyncSession) -> object:
    from app.models import User

    user = await session.scalar(sa.select(User).limit(1))
    assert user is not None
    return user.id


async def test_second_search_is_served_from_the_cache(
    seeded_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    cache = InMemoryCache()
    knowledge = KnowledgeSearch(cache, ttl_seconds=60)
    async with session_factory() as session:
        uid = await owner_id(session)
        await ingest_document(session, uid, "Travel", TRAVEL)  # type: ignore[arg-type]
        await session.commit()
        before = (retrieval("hit"), retrieval("miss"))
        first = await knowledge.search(session, "Hotel nightly cap", 5)
        # same words, different case and spacing -> same cache entry
        second = await knowledge.search(session, "  hotel   NIGHTLY cap ", 5)
    assert first and first == second
    assert retrieval("miss") - before[1] == 1 and retrieval("hit") - before[0] == 1


async def test_top_k_is_part_of_the_cache_key(
    seeded_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    knowledge = KnowledgeSearch(InMemoryCache())
    async with session_factory() as session:
        uid = await owner_id(session)
        for n in range(3):
            await ingest_document(session, uid, f"Doc {n}", f"refund policy variant number {n}")  # type: ignore[arg-type]
        await session.commit()
        one = await knowledge.search(session, "refund policy", 1)
        three = await knowledge.search(session, "refund policy", 3)
    assert len(one) == 1 and len(three) == 3


async def test_a_corrupt_entry_is_recomputed(
    seeded_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    cache = InMemoryCache()
    knowledge = KnowledgeSearch(cache)
    async with session_factory() as session:
        uid = await owner_id(session)
        await ingest_document(session, uid, "Travel", TRAVEL)  # type: ignore[arg-type]
        await session.commit()
        good = await knowledge.search(session, "hotel cap", 5)
        key = next(k for k in cache._data if k.startswith("kb:0:"))
        await cache.set(key, json.dumps([{"unexpected": "shape"}]), 60)
        assert await knowledge.search(session, "hotel cap", 5) == good


async def test_without_a_cache_search_still_works(
    seeded_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    knowledge = KnowledgeSearch()
    await knowledge.invalidate()  # harmless
    async with session_factory() as session:
        uid = await owner_id(session)
        await ingest_document(session, uid, "Travel", TRAVEL)  # type: ignore[arg-type]
        await session.commit()
        assert await knowledge.search(session, "hotel cap") != []


async def test_uploading_and_deleting_documents_invalidates_cached_searches(
    seeded_client: AsyncClient,
) -> None:
    h = bearer(await login(seeded_client, "operator@demo.local"))
    search = f"{BASE}/search?q=parking permits"

    assert (await seeded_client.get(search, headers=h)).json() == []
    assert (await seeded_client.get(search, headers=h)).json() == []  # now cached as "empty"

    created = await seeded_client.post(
        BASE,
        json={"title": "Parking", "content": "Parking permits are issued by facilities."},
        headers=h,
    )
    assert created.status_code == 201
    found = (await seeded_client.get(search, headers=h)).json()
    assert [p["title"] for p in found] == ["Parking"]  # the cached "empty" answer was not served

    doc_id = created.json()["document"]["id"]
    assert (await seeded_client.delete(f"{BASE}/{doc_id}", headers=h)).status_code == 204
    assert (await seeded_client.get(search, headers=h)).json() == []  # deleted doc is gone
