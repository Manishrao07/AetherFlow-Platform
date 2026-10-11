"""LongTermMemory and knowledge search services."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.long_term import LongTermMemory
from app.models import Document, DocumentChunk, User
from app.models.enums import MemoryKind
from app.services.knowledge import search_chunks


async def make_user(session: AsyncSession, email: str) -> uuid.UUID:
    user = User(email=email, full_name="T", password_hash="x")
    session.add(user)
    await session.flush()
    return user.id


async def test_upsert_updates_in_place(session: AsyncSession) -> None:
    uid = await make_user(session, "m1@test.dev")
    memory = LongTermMemory(session)
    a = await memory.upsert(uid, "tone", "likes formal tone", MemoryKind.PREFERENCE, 2)
    b = await memory.upsert(uid, "tone", "likes casual tone", MemoryKind.PREFERENCE, 4)
    assert a.id == b.id and b.content == "likes casual tone" and b.importance == 4
    rows, total = await memory.list(uid)
    assert total == 1 and rows[0].content == "likes casual tone"


async def test_search_ranks_by_relevance_and_importance_with_or_semantics(
    session: AsyncSession,
) -> None:
    uid = await make_user(session, "m2@test.dev")
    memory = LongTermMemory(session)
    await memory.upsert(uid, "a", "customer prefers email reports every week", importance=1)
    await memory.upsert(uid, "b", "weekly report goes to finance", importance=5)
    await memory.upsert(uid, "c", "office is in Pune", importance=5)
    hits = await memory.search(uid, "weekly report preferences please", top_k=5)
    keys = [m.key for m in hits]
    assert "c" not in keys and keys[0] == "b"  # best match first; unrelated memory excluded
    await session.refresh(hits[0])
    assert hits[0].last_accessed_at is not None


async def test_empty_query_returns_most_important(session: AsyncSession) -> None:
    uid = await make_user(session, "m3@test.dev")
    memory = LongTermMemory(session)
    await memory.upsert(uid, "low", "minor thing", importance=1)
    await memory.upsert(uid, "high", "major thing", importance=5)
    assert [m.key for m in await memory.search(uid, "  ", top_k=1)] == ["high"]
    assert await memory.search(uid, "!!! ???") == []  # query with no searchable words


async def test_delete_and_privacy_erase_are_user_scoped(session: AsyncSession) -> None:
    a, b = await make_user(session, "m4@test.dev"), await make_user(session, "m5@test.dev")
    memory = LongTermMemory(session)
    for uid in (a, b):
        await memory.upsert(uid, "k1", "one")
        await memory.upsert(uid, "k2", "two")
    assert await memory.delete(a, "k1") is True and await memory.delete(a, "k1") is False
    assert await memory.delete_all(a) == 1
    _, remaining_b = await memory.list(b)
    assert remaining_b == 2  # other user untouched


async def test_knowledge_search_ranking_and_status_filter(session: AsyncSession) -> None:
    uid = await make_user(session, "k1@test.dev")
    ready = Document(owner_id=uid, title="Handbook", content_sha256="a" * 64, chunk_count=2)
    failed = Document(
        owner_id=uid, title="Broken", content_sha256="b" * 64, status="FAILED", chunk_count=1
    )
    session.add_all([ready, failed])
    await session.flush()
    session.add_all(
        [
            DocumentChunk(
                document_id=ready.id, chunk_index=0, content="Refunds are issued within seven days"
            ),
            DocumentChunk(
                document_id=ready.id, chunk_index=1, content="Office plants need weekly watering"
            ),
            DocumentChunk(
                document_id=failed.id, chunk_index=0, content="Refunds refunds refunds secret"
            ),
        ]
    )
    await session.flush()
    results = await search_chunks(session, "when are refunds issued", top_k=5)
    assert [r.title for r in results] == ["Handbook"] and results[0].chunk_index == 0
    assert results[0].citation == "Handbook #1" and results[0].score > 0
    assert await search_chunks(session, "   ") == []
    assert len(await search_chunks(session, "refunds weekly watering", top_k=1)) == 1


async def test_search_understands_listed_synonyms(session: AsyncSession) -> None:
    uid = await make_user(session, "k2@test.dev")
    doc = Document(owner_id=uid, title="Policy", content_sha256="c" * 64, chunk_count=1)
    session.add(doc)
    await session.flush()
    session.add(
        DocumentChunk(document_id=doc.id, chunk_index=0, content="Refunds are paid within 7 days")
    )
    await session.flush()
    results = await search_chunks(session, "how do I get my money back", top_k=3)
    assert [r.title for r in results] == ["Policy"]
