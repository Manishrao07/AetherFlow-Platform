"""Long-term memory endpoints: list, search, upsert, delete one, erase all (privacy)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from app.api.deps import AuditDep, ClientIpDep, LongTermMemoryDep, require_permission_dep
from app.api.schemas.common import Page, PageParams, page_params
from app.api.schemas.memory import ErasedOut, MemoryOut, MemoryUpsert
from app.core.errors import NotFoundError
from app.core.rbac import Principal
from app.services.permissions import MEMORY_READ, MEMORY_WRITE

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

ReaderDep = Annotated[Principal, Depends(require_permission_dep(MEMORY_READ))]
WriterDep = Annotated[Principal, Depends(require_permission_dep(MEMORY_WRITE))]
PageDep = Annotated[PageParams, Depends(page_params)]
KeyPath = Annotated[str, Path(min_length=1, max_length=120)]


@router.get("", response_model=Page[MemoryOut])
async def list_memories(
    user: ReaderDep, memory: LongTermMemoryDep, page: PageDep
) -> Page[MemoryOut]:
    """Your remembered facts and preferences (never anyone else's)."""
    rows, total = await memory.list(user.user_id, page.limit, page.offset)
    return Page(
        items=[MemoryOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/search", response_model=list[MemoryOut])
async def search_memories(
    user: ReaderDep,
    memory: LongTermMemoryDep,
    q: Annotated[str, Query(max_length=200)] = "",
    top_k: Annotated[int, Query(ge=1, le=20)] = 5,
) -> list[MemoryOut]:
    """Most relevant memories for ``q`` (empty ``q`` = most important)."""
    return [MemoryOut.model_validate(m) for m in await memory.search(user.user_id, q, top_k)]


@router.put("/{key}", response_model=MemoryOut)
async def upsert_memory(
    key: KeyPath, body: MemoryUpsert, user: WriterDep, memory: LongTermMemoryDep
) -> MemoryOut:
    """Create or update a memory by key (same key = overwrite, never a duplicate)."""
    row = await memory.upsert(user.user_id, key, body.content, body.kind, body.importance)
    return MemoryOut.model_validate(row)


# WHY delete needs only MEMORY_READ: erasing your own data is a privacy right every account
# holds, including read-only viewers.
@router.delete("/{key}", status_code=204)
async def delete_memory(
    key: KeyPath, user: ReaderDep, memory: LongTermMemoryDep, audit: AuditDep, ip: ClientIpDep
) -> Response:
    if not await memory.delete(user.user_id, key):
        raise NotFoundError("Memory not found")
    await audit.record(
        "memory.deleted", actor_id=user.user_id, actor_email=user.email,
        resource_type="memory", resource_id=key, ip_address=ip,
    )  # fmt: skip
    return Response(status_code=204)


@router.delete("", response_model=ErasedOut)
async def erase_all_memories(
    user: ReaderDep, memory: LongTermMemoryDep, audit: AuditDep, ip: ClientIpDep
) -> ErasedOut:
    """Erase EVERY memory of the caller (audited)."""
    deleted = await memory.delete_all(user.user_id)
    await audit.record(
        "memory.erased", actor_id=user.user_id, actor_email=user.email,
        resource_type="memory", details={"deleted": deleted}, ip_address=ip,
    )  # fmt: skip
    return ErasedOut(deleted=deleted)
