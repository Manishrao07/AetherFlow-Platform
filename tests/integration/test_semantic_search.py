"""Hybrid search, embedding at ingest, and the startup backfill (real Postgres)."""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.embeddings import Embedder, EmbeddingError, HashingEmbedder
from app.models import DocumentChunk, User
from app.services.documents import backfill_embeddings, ingest_document
from app.services.knowledge import search_chunks

REFUND = "Refunds are issued to the original payment method within five business days."
HOTEL = "Hotel stays are capped at 200 USD per night unless a manager approves."


def paraphrase(text: str) -> str:
    # A tiny stand-in for "the model knows cash back means refund".
    return text.replace("cash back", "refund")


class FailingEmbedder(Embedder):
    name = "failing"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("down")


async def owner(session: AsyncSession) -> uuid.UUID:
    user = User(email=f"{uuid.uuid4().hex[:8]}@test.dev", full_name="T", password_hash="x")
    session.add(user)
    await session.flush()
    return user.id


async def test_ingest_stores_vectors_and_the_model_name(session: AsyncSession) -> None:
    embedder = HashingEmbedder()
    await ingest_document(session, await owner(session), "Refunds", REFUND, embedder=embedder)
    chunk = await session.scalar(sa.select(DocumentChunk))
    assert chunk is not None and chunk.embedding_model == embedder.name
    assert chunk.embedding is not None and len(chunk.embedding) == 512


async def test_semantic_hit_when_no_keyword_matches(session: AsyncSession) -> None:
    embedder = HashingEmbedder(expand=paraphrase)
    uid = await owner(session)
    await ingest_document(session, uid, "Refunds", REFUND, embedder=embedder)
    await ingest_document(session, uid, "Travel", HOTEL, embedder=embedder)
    assert await search_chunks(session, "cash back", 3) == []  # keyword search alone: nothing
    hits = await search_chunks(session, "cash back", 3, embedder)
    assert [h.title for h in hits] == ["Refunds"] and 0.2 <= hits[0].score <= 1.0


async def test_unrelated_query_returns_nothing_even_with_embeddings(session: AsyncSession) -> None:
    embedder = HashingEmbedder()
    await ingest_document(session, await owner(session), "Refunds", REFUND, embedder=embedder)
    assert await search_chunks(session, "quantum chromodynamics lattice", 3, embedder) == []


async def test_hybrid_ranks_a_chunk_found_by_both_methods_first(session: AsyncSession) -> None:
    embedder = HashingEmbedder()
    uid = await owner(session)
    await ingest_document(session, uid, "Travel", HOTEL, embedder=embedder)
    await ingest_document(session, uid, "Refunds", REFUND, embedder=embedder)
    hits = await search_chunks(session, "refunds original payment method", 5, embedder)
    assert hits[0].title == "Refunds"


async def test_vectors_from_another_model_are_ignored(session: AsyncSession) -> None:
    uid = await owner(session)
    await ingest_document(session, uid, "Refunds", REFUND, embedder=HashingEmbedder(256))
    semantic_only = await search_chunks(session, "zzz", 3, HashingEmbedder(512), min_similarity=0.0)
    assert semantic_only == []  # no chunk carries a hash-512 vector


async def test_failed_embedding_at_ingest_keeps_the_document(session: AsyncSession) -> None:
    result = await ingest_document(
        session, await owner(session), "Refunds", REFUND, embedder=FailingEmbedder()
    )
    assert result.created
    chunk = await session.scalar(sa.select(DocumentChunk))
    assert chunk is not None and chunk.embedding is None
    assert (await search_chunks(session, "refunds", 3))[0].title == "Refunds"


async def test_failed_query_embedding_falls_back_to_keywords(session: AsyncSession) -> None:
    await ingest_document(session, await owner(session), "Refunds", REFUND)
    hits = await search_chunks(session, "refunds", 3, FailingEmbedder())
    assert [h.title for h in hits] == ["Refunds"]


async def test_backfill_fills_missing_vectors_and_is_repeatable(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    uid = await owner(session)
    await ingest_document(session, uid, "Refunds", REFUND)
    await ingest_document(session, uid, "Travel", HOTEL)
    await session.commit()
    embedder = HashingEmbedder()
    assert await backfill_embeddings(session_factory, embedder, batch_size=1) == 2
    assert await backfill_embeddings(session_factory, embedder) == 0
    # A different model name means the old vectors are stale and are re-embedded.
    assert await backfill_embeddings(session_factory, HashingEmbedder(128)) == 2


async def test_backfill_stops_quietly_when_the_service_is_down(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await ingest_document(session, await owner(session), "Refunds", REFUND)
    await session.commit()
    assert await backfill_embeddings(session_factory, FailingEmbedder()) == 0
