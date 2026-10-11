"""Knowledge-base search over document chunks (Postgres full-text search).

What: ``search_chunks`` returns the top-k most relevant chunks WITH citation data.
Why full-text search: deterministic, no embedding provider needed, works offline with the
mock LLM, and a GIN index keeps it fast. Trade-off: it matches words, not meaning - see
docs/tradeoffs.md for the pgvector upgrade path (same function signature).
Why OR-semantics + ``ts_rank_cd``: natural-language questions contain filler words; requiring
ALL terms would return nothing. We match any term and let ranking put the best chunks first.
Caching: ``KnowledgeSearch`` puts a TTL cache in front of ``search_chunks``. Searches are the
hottest read path (every retrieval step and every Search box), results are the same for every
user (the knowledge base is shared for reading), and documents change rarely.
Invalidation: ingest/delete bump a version number that is part of every cache key, so stale
entries are never read again (they simply expire). The version lives in the cache backend, so
with Redis all API replicas see it; with the in-process cache, other replicas converge within
``RETRIEVAL_CACHE_TTL_SECONDS``.
Hybrid search: when an embedder is configured, keyword candidates and vector candidates are
merged with reciprocal-rank fusion (RRF), so a chunk that is good by words, by meaning, or both
surfaces. The reported score is the cosine similarity (0..1) when the chunk has a vector.
Connects to: ``search_knowledge_base`` tool and RetrievalAgent (Phase 4).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache, MeteredCache
from app.core.embeddings import Embedder, EmbeddingError, cosine
from app.models import Document, DocumentChunk
from app.models.enums import DocumentStatus
from app.services.synonyms import expand_query

logger = logging.getLogger(__name__)

_FUSION_K = 60  # standard RRF constant: damps the advantage of rank 1 over rank 2
_CANDIDATES = 20  # how many keyword hits to fuse

_TSQUERY = sa.func.to_tsquery(
    "english",
    sa.func.replace(
        sa.cast(sa.func.plainto_tsquery("english", sa.bindparam("q")), sa.Text), "&", "|"
    ),
)


@dataclass(frozen=True)
class Passage:
    document_id: uuid.UUID
    title: str
    chunk_index: int
    content: str
    score: float

    @property
    def citation(self) -> str:
        return f"{self.title} #{self.chunk_index + 1}"


async def search_chunks(
    session: AsyncSession,
    query: str,
    top_k: int = 5,
    embedder: Embedder | None = None,
    *,
    min_similarity: float = 0.2,
    max_candidates: int = 5000,
) -> list[Passage]:
    """Rank READY document chunks against ``query``; empty list when nothing matches.

    Keyword-only without an embedder (or if embedding the query fails); hybrid with one.
    """
    if not query.strip():
        return []
    keyword = await _keyword_hits(session, query, max(top_k, _CANDIDATES))
    if embedder is None:
        return keyword[:top_k]
    try:
        (query_vector,) = await embedder.embed([query])
    except EmbeddingError:
        logger.warning("query embedding failed; using keyword search only", exc_info=True)
        return keyword[:top_k]
    semantic = await _semantic_hits(
        session, query_vector, embedder.name, max(top_k, _CANDIDATES), min_similarity,
        max_candidates,
    )  # fmt: skip
    return _fuse(keyword, semantic)[:top_k]


async def _keyword_hits(session: AsyncSession, query: str, limit: int) -> list[Passage]:
    tsquery = _TSQUERY.params(q=expand_query(query))
    rank = sa.func.ts_rank_cd(DocumentChunk.search_vector, tsquery).label("score")
    stmt = (
        sa.select(
            Document.id, Document.title, DocumentChunk.chunk_index, DocumentChunk.content, rank
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            Document.status == DocumentStatus.READY, DocumentChunk.search_vector.op("@@")(tsquery)
        )
        .order_by(rank.desc(), Document.id, DocumentChunk.chunk_index)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [Passage(r[0], r[1], r[2], r[3], float(r[4])) for r in rows]


async def _semantic_hits(
    session: AsyncSession,
    query_vector: list[float],
    model: str,
    limit: int,
    min_similarity: float,
    max_candidates: int,
) -> list[Passage]:
    """Top chunks by cosine similarity among chunks embedded with the SAME model."""
    stmt = (
        sa.select(
            Document.id,
            Document.title,
            DocumentChunk.chunk_index,
            DocumentChunk.content,
            DocumentChunk.embedding,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.status == DocumentStatus.READY, DocumentChunk.embedding_model == model)
        .order_by(Document.id, DocumentChunk.chunk_index)
        .limit(max_candidates)
    )
    scored = [
        Passage(r[0], r[1], r[2], r[3], cosine(query_vector, r[4] or []))
        for r in (await session.execute(stmt)).all()
    ]
    best = sorted((p for p in scored if p.score >= min_similarity), key=lambda p: -p.score)
    return best[:limit]


def _fuse(keyword: list[Passage], semantic: list[Passage]) -> list[Passage]:
    """Reciprocal-rank fusion of the two ranked lists; keyword-only hits keep their rank score.

    WHY RRF: the two scores live on different scales (ts_rank vs cosine); ranks do not.
    """
    fused: dict[tuple[uuid.UUID, int], float] = {}
    by_key: dict[tuple[uuid.UUID, int], Passage] = {}
    for ranked in (keyword, semantic):
        for rank, passage in enumerate(ranked):
            key = (passage.document_id, passage.chunk_index)
            fused[key] = fused.get(key, 0.0) + 1.0 / (_FUSION_K + rank)
            # WHY prefer the semantic copy: its score is a cosine in 0..1 (easier to read).
            if key not in by_key or ranked is semantic:
                by_key[key] = passage
    return [by_key[k] for k in sorted(fused, key=lambda k: (-fused[k], str(k[0]), k[1]))]


_VERSION_KEY = "kb:version"


class KnowledgeSearch:
    """``search_chunks`` with an optional cache in front (pass-through when ``cache`` is None)."""

    def __init__(
        self,
        cache: Cache | None = None,
        ttl_seconds: int = 60,
        embedder: Embedder | None = None,
        min_similarity: float = 0.2,
        max_candidates: int = 5000,
    ) -> None:
        self.embedder = embedder
        self._min_similarity = min_similarity
        self._max_candidates = max_candidates
        self._raw = cache  # the version lookup must not count as a cache hit/miss
        self._cache = MeteredCache(cache, "retrieval") if cache is not None else None
        self._ttl = ttl_seconds

    async def search(self, session: AsyncSession, query: str, top_k: int = 5) -> list[Passage]:
        if self._cache is None:
            return await self._run(session, query, top_k)
        key = await self._key(query, top_k)
        cached = await self._cache.get(key)
        if cached is not None:
            try:
                return [
                    Passage(uuid.UUID(p["document_id"]), p["title"], p["chunk_index"],
                            p["content"], p["score"])
                    for p in json.loads(cached)
                ]  # fmt: skip
            except (ValueError, KeyError, TypeError):
                pass  # corrupt entry: fall through and recompute
        passages = await self._run(session, query, top_k)
        payload = [{**asdict(p), "document_id": str(p.document_id)} for p in passages]
        await self._cache.set(key, json.dumps(payload), self._ttl)
        return passages

    async def _run(self, session: AsyncSession, query: str, top_k: int) -> list[Passage]:
        return await search_chunks(
            session,
            query,
            top_k,
            self.embedder,
            min_similarity=self._min_similarity,
            max_candidates=self._max_candidates,
        )

    async def invalidate(self) -> None:
        """Call after documents are added or removed."""
        if self._raw is not None:
            await self._raw.incr(_VERSION_KEY)

    async def _key(self, query: str, top_k: int) -> str:
        assert self._raw is not None
        version = await self._raw.get(_VERSION_KEY) or "0"
        # WHY normalise: "Refund Policy" and "refund  policy" rank identically in full-text search.
        normalised = " ".join(query.lower().split())
        mode = self.embedder.name if self.embedder else "keyword"
        digest = hashlib.sha256(f"{normalised}|{top_k}|{mode}".encode()).hexdigest()
        return f"kb:{version}:{digest}"
