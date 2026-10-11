"""Knowledge base (documents + chunks) and long-term memory.

What: ``Document``, ``DocumentChunk`` and ``Memory``; chunks and memories carry a generated
``tsvector`` column for full-text relevance search.
Why Postgres full-text search: no embedding provider or extra service is needed, results
are deterministic (testable with the mock LLM), and a GIN index makes it fast. The
``Retriever`` interface (Phase 4) lets us swap in pgvector later. See docs/tradeoffs.md.
Connects to: RetrievalAgent / ingestion service, memory tools, privacy delete endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_column
from app.models.enums import DocumentStatus, MemoryKind


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    source: Mapped[str] = mapped_column(sa.String(500), nullable=False, server_default="upload")
    # WHY hash: re-uploading identical content is detected and ignored (idempotent ingestion).
    content_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        enum_column(DocumentStatus, "document_status"),
        nullable=False,
        default=DocumentStatus.READY,
        server_default=DocumentStatus.READY.value,
    )
    chunk_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")

    __table_args__ = (
        sa.CheckConstraint("chunk_count >= 0", name="chunk_count_non_negative"),
        sa.UniqueConstraint("owner_id", "content_sha256", name="uq_documents_owner_sha256"),
    )


class DocumentChunk(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "document_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    token_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    # WHY generated column: Postgres keeps the search vector in sync automatically, so
    # application code can never forget to update it.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, sa.Computed("to_tsvector('english', content)", persisted=True), nullable=False
    )

    # WHY a plain float array and not pgvector: it needs no Postgres extension (so the stock
    # image works and tests run anywhere); similarity is computed in the application. The column
    # can be swapped for ``vector`` + an HNSW index when the corpus outgrows that
    # (docs/tradeoffs.md). ``embedding_model`` records which embedder produced the vector:
    # vectors from different models are not comparable, so search only uses matching ones.
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(sa.REAL), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(sa.String(80), nullable=True)

    __table_args__ = (
        sa.CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_document_index"),
        # WHY GIN: the standard index type for tsvector; makes `@@` queries index-driven.
        sa.Index("ix_document_chunks_search", "search_vector", postgresql_using="gin"),
        sa.Index(
            "ix_document_chunks_unembedded",
            "id",
            postgresql_where=sa.text("embedding IS NULL"),
        ),
    )


class Memory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A long-term fact/preference about one user (e.g. 'prefers weekly summaries')."""

    __tablename__ = "memories"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[MemoryKind] = mapped_column(
        enum_column(MemoryKind, "memory_kind"),
        nullable=False,
        default=MemoryKind.FACT,
        server_default=MemoryKind.FACT.value,
    )
    importance: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="1")
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        sa.Computed("to_tsvector('english', key || ' ' || content)", persisted=True),
        nullable=False,
    )

    __table_args__ = (
        sa.CheckConstraint("importance BETWEEN 1 AND 5", name="importance_range"),
        # WHY unique(user, key): writing the same fact twice updates it (upsert) - no duplicates.
        sa.UniqueConstraint("user_id", "key", name="uq_memories_user_key"),
        sa.Index("ix_memories_search", "search_vector", postgresql_using="gin"),
    )
