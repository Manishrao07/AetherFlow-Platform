"""Document ingestion and management (the write side of the knowledge base).

What: ``ingest_document`` cleans text, de-duplicates by content hash, chunks it and stores the
chunks (Postgres generates the search vectors); ``DocumentService`` lists, searches and deletes.
Why idempotent ingestion: uploading the same file twice (double click, retried request) must
not create duplicate chunks that would crowd real results. The unique (owner, sha256) index
enforces it in the database, not just in application code.
Why delete is owner-or-admin: the knowledge base is shared for reading, but a user must not be
able to erase documents other people uploaded.
Connects to: documents router, seeding, RetrievalAgent (reads via ``services/knowledge.py``).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.schemas.common import PageParams
from app.core.audit import AuditService
from app.core.embeddings import Embedder, EmbeddingError
from app.core.errors import AppError, NotFoundError, PermissionDeniedError
from app.core.rbac import Principal
from app.models import Document, DocumentChunk
from app.models.enums import DocumentStatus, RoleName
from app.services.chunking import chunk_text, estimate_tokens
from app.services.knowledge import KnowledgeSearch, Passage


class EmptyDocumentError(AppError):
    status_code = 422
    code = "empty_document"


@dataclass(frozen=True)
class IngestResult:
    document: Document
    created: bool  # False when identical content was already ingested by this owner


def clean_text(text: str) -> str:
    """Normalise newlines and drop NUL bytes (Postgres cannot store them in text)."""
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()


logger = logging.getLogger(__name__)


async def _embed_or_none(embedder: Embedder | None, chunks: list[str]) -> list[list[float]] | None:
    """Vectors for ``chunks``; ``None`` if disabled or the service is down (backfill retries)."""
    if embedder is None:
        return None
    try:
        return await embedder.embed(chunks)
    except EmbeddingError:
        # WHY not fail the upload: keyword search works without vectors, and the startup
        # backfill fills them in later.
        logger.warning("could not embed a new document; it is keyword-searchable only")
        return None


async def ingest_document(
    session: AsyncSession,
    owner_id: uuid.UUID,
    title: str,
    content: str,
    source: str = "upload",
    embedder: Embedder | None = None,
) -> IngestResult:
    """Store ``content`` as a document with chunks; no-op if the owner already has it."""
    text = clean_text(content)
    chunks = chunk_text(text) if text else []
    if not chunks:
        raise EmptyDocumentError("Document has no text content")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = await session.scalar(
        sa.select(Document).where(Document.owner_id == owner_id, Document.content_sha256 == digest)
    )
    if existing is not None:
        return IngestResult(existing, created=False)
    document = Document(
        owner_id=owner_id,
        title=title.strip(),
        source=source,
        content_sha256=digest,
        status=DocumentStatus.READY,
        chunk_count=len(chunks),
    )
    session.add(document)
    await session.flush()
    vectors = await _embed_or_none(embedder, chunks)
    session.add_all(
        DocumentChunk(
            document_id=document.id,
            chunk_index=index,
            content=chunk,
            token_count=estimate_tokens(chunk),
            embedding=vectors[index] if vectors else None,
            embedding_model=embedder.name if vectors and embedder else None,
        )
        for index, chunk in enumerate(chunks)
    )
    await session.flush()
    return IngestResult(document, created=True)


class DocumentService:
    def __init__(
        self, session: AsyncSession, audit: AuditService, knowledge: KnowledgeSearch | None = None
    ) -> None:
        self._session = session
        self._audit = audit
        self._knowledge = knowledge or KnowledgeSearch()

    async def ingest(
        self, principal: Principal, title: str, content: str, ip: str | None
    ) -> IngestResult:
        result = await ingest_document(
            self._session, principal.user_id, title, content, embedder=self._knowledge.embedder
        )
        # WHY commit before auditing: the audit writer uses its own connection; the document
        # row must be visible/durable first so the log never describes something that rolled back.
        await self._session.commit()
        if result.created:
            await self._knowledge.invalidate()
            await self._audit.record(
                "document.ingested", actor_id=principal.user_id, actor_email=principal.email,
                resource_type="document", resource_id=result.document.id, ip_address=ip,
                details={"title": result.document.title, "chunks": result.document.chunk_count},
            )  # fmt: skip
        return result

    async def list_documents(self, page: PageParams) -> tuple[list[Document], int]:
        total = await self._session.scalar(sa.select(sa.func.count()).select_from(Document)) or 0
        rows = await self._session.scalars(
            sa.select(Document)
            .order_by(Document.created_at.desc(), Document.id)
            .limit(page.limit)
            .offset(page.offset)
        )
        return list(rows), total

    async def get(self, document_id: uuid.UUID) -> Document:
        document = await self._session.get(Document, document_id)
        if document is None:
            raise NotFoundError("Document not found")
        return document

    async def search(self, query: str, top_k: int) -> list[Passage]:
        return await self._knowledge.search(self._session, query, top_k)

    async def delete(self, principal: Principal, document_id: uuid.UUID, ip: str | None) -> None:
        document = await self.get(document_id)
        if document.owner_id != principal.user_id and RoleName.ADMIN.value not in principal.roles:
            raise PermissionDeniedError("Only the owner or an admin can delete this document")
        title = document.title
        await self._session.delete(document)  # chunks go with it (ON DELETE CASCADE)
        await self._session.commit()
        await self._knowledge.invalidate()
        await self._audit.record(
            "document.deleted", actor_id=principal.user_id, actor_email=principal.email,
            resource_type="document", resource_id=document_id, ip_address=ip,
            details={"title": title},
        )  # fmt: skip


async def backfill_embeddings(
    session_factory: async_sessionmaker[AsyncSession], embedder: Embedder, batch_size: int = 64
) -> int:
    """Embed chunks that have no vector, or were embedded by a different model. Returns count.

    WHY: documents ingested before embeddings existed, uploads made while the embedding service
    was down, and a changed EMBEDDING_MODEL all leave chunks that semantic search cannot see.
    Runs at startup and is safe to repeat (it only touches chunks that need it).
    """
    done = 0
    while True:
        async with session_factory() as session, session.begin():
            rows = (
                await session.execute(
                    sa.select(DocumentChunk.id, DocumentChunk.content)
                    .where(
                        sa.or_(
                            DocumentChunk.embedding.is_(None),
                            DocumentChunk.embedding_model != embedder.name,
                        )
                    )
                    .order_by(DocumentChunk.id)
                    .limit(batch_size)
                )
            ).all()
            if not rows:
                return done
            try:
                vectors = await embedder.embed([r[1] for r in rows])
            except EmbeddingError:
                logger.warning("embedding backfill stopped: service unavailable")
                return done
            for (chunk_id, _), vector in zip(rows, vectors, strict=True):
                await session.execute(
                    sa.update(DocumentChunk)
                    .where(DocumentChunk.id == chunk_id)
                    .values(embedding=vector, embedding_model=embedder.name)
                )
            done += len(rows)
