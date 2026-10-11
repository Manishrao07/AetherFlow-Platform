"""Long-term memory: durable per-user facts and preferences in Postgres.

What: upsert / relevance-search / list / delete for the ``memories`` table.
Why Postgres (not a separate vector store): memories are small, per-user, and need
transactions, privacy deletion and exact ownership checks - all things a relational DB does
natively. Full-text ranking (``ts_rank_cd``) over a generated ``tsvector`` finds relevant
memories without an embedding service. Scale-out path: pgvector later (docs/tradeoffs.md).
Why every method takes ``user_id``: memory is strictly private; there is no query that can
cross users, so a prompt-injected agent cannot read someone else's data.
Connects to: memory tools (agents read/write through them) and the memory API (Phase 4).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Memory
from app.models.enums import MemoryKind

# OR-semantics query: "weekly report preference" should match a memory containing just
# "weekly"; ranking then prefers memories matching more terms.
_OR_QUERY = sa.func.to_tsquery(
    "english",
    sa.func.replace(
        sa.cast(sa.func.plainto_tsquery("english", sa.bindparam("q")), sa.Text), "&", "|"
    ),
)


class LongTermMemory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        user_id: uuid.UUID,
        key: str,
        content: str,
        kind: MemoryKind = MemoryKind.FACT,
        importance: int = 1,
    ) -> Memory:
        """Create the memory, or update it if this user already has the same key."""
        stmt = (
            insert(Memory)
            .values(user_id=user_id, key=key, content=content, kind=kind, importance=importance)
            .on_conflict_do_update(
                constraint="uq_memories_user_key",
                set_={
                    "content": content,
                    "kind": kind,
                    "importance": importance,
                    "updated_at": sa.func.now(),
                },
            )
            .returning(Memory)
            # WHY populate_existing: if this session already holds the row, refresh it with
            # the values just written instead of returning the stale cached object.
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def search(self, user_id: uuid.UUID, query: str, top_k: int = 5) -> list[Memory]:
        """Most relevant memories for ``query`` (or the most important ones if query is empty)."""
        if not query.strip():
            stmt = (
                sa.select(Memory)
                .where(Memory.user_id == user_id)
                .order_by(Memory.importance.desc(), Memory.updated_at.desc())
                .limit(top_k)
            )
        else:
            tsquery = _OR_QUERY.params(q=query)
            rank = sa.func.ts_rank_cd(Memory.search_vector, tsquery)
            stmt = (
                sa.select(Memory)
                .where(Memory.user_id == user_id, Memory.search_vector.op("@@")(tsquery))
                .order_by((rank * Memory.importance).desc(), Memory.updated_at.desc())
                .limit(top_k)
            )
        rows = list((await self._session.scalars(stmt)).all())
        if rows:
            await self._session.execute(
                sa.update(Memory)
                .where(Memory.id.in_([m.id for m in rows]))
                .values(last_accessed_at=sa.func.now())
                # WHY False: don't expire the rows we are about to return (async sessions
                # cannot lazily reload attributes).
                .execution_options(synchronize_session=False)
            )
        return rows

    async def list(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> tuple[list[Memory], int]:
        total = (
            await self._session.scalar(
                sa.select(sa.func.count()).select_from(Memory).where(Memory.user_id == user_id)
            )
            or 0
        )
        rows = await self._session.scalars(
            sa.select(Memory)
            .where(Memory.user_id == user_id)
            .order_by(Memory.updated_at.desc(), Memory.id)
            .limit(limit)
            .offset(offset)
        )
        return list(rows), total

    async def delete(self, user_id: uuid.UUID, key: str) -> bool:
        result = await self._session.execute(
            sa.delete(Memory).where(Memory.user_id == user_id, Memory.key == key)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def delete_all(self, user_id: uuid.UUID) -> int:
        """Privacy erase: remove every memory of this user."""
        result = await self._session.execute(sa.delete(Memory).where(Memory.user_id == user_id))
        return int(result.rowcount)  # type: ignore[attr-defined]
