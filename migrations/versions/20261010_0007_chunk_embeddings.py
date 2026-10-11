"""Embeddings for semantic search: a float array and the model name on each chunk.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_chunks", sa.Column("embedding", postgresql.ARRAY(sa.REAL()), nullable=True)
    )
    op.add_column(
        "document_chunks", sa.Column("embedding_model", sa.String(length=80), nullable=True)
    )
    # WHY partial: the backfill job looks for chunks that still need a vector; once all have
    # one this index is empty.
    op.create_index(
        "ix_document_chunks_unembedded",
        "document_chunks",
        ["id"],
        postgresql_where=sa.text("embedding IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_unembedded", table_name="document_chunks")
    op.drop_column("document_chunks", "embedding_model")
    op.drop_column("document_chunks", "embedding")
