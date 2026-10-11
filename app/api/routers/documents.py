"""Knowledge-base endpoints: ingest, list, get, search, delete."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import ClientIpDep, DocumentServiceDep, require_permission_dep
from app.api.schemas.common import Page, PageParams, page_params
from app.api.schemas.documents import DocumentCreate, DocumentOut, IngestOut, PassageOut
from app.core.rbac import Principal
from app.services.permissions import DOCUMENT_READ, DOCUMENT_WRITE

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

ReaderDep = Annotated[Principal, Depends(require_permission_dep(DOCUMENT_READ))]
WriterDep = Annotated[Principal, Depends(require_permission_dep(DOCUMENT_WRITE))]
PageDep = Annotated[PageParams, Depends(page_params)]


@router.post("", response_model=IngestOut, status_code=201)
async def ingest_document(
    body: DocumentCreate,
    response: Response,
    user: WriterDep,
    service: DocumentServiceDep,
    ip: ClientIpDep,
) -> IngestOut:
    """Chunk and store a text document. Re-uploading identical content returns 200, not 201."""
    result = await service.ingest(user, body.title, body.content, ip)
    if not result.created:
        response.status_code = 200
    return IngestOut(document=DocumentOut.model_validate(result.document), created=result.created)


@router.get("", response_model=Page[DocumentOut])
async def list_documents(
    _: ReaderDep, service: DocumentServiceDep, page: PageDep
) -> Page[DocumentOut]:
    rows, total = await service.list_documents(page)
    return Page(
        items=[DocumentOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/search", response_model=list[PassageOut])
async def search_documents(
    _: ReaderDep,
    service: DocumentServiceDep,
    q: Annotated[str, Query(min_length=1, max_length=500, description="Natural-language query")],
    top_k: Annotated[int, Query(ge=1, le=10)] = 5,
) -> list[PassageOut]:
    """Top-k passages with citations (the same search the RetrievalAgent uses)."""
    passages = await service.search(q, top_k)
    return [
        PassageOut(
            document_id=p.document_id, title=p.title, chunk_index=p.chunk_index,
            citation=p.citation, score=round(p.score, 4), content=p.content,
        )
        for p in passages
    ]  # fmt: skip


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID, _: ReaderDep, service: DocumentServiceDep
) -> DocumentOut:
    return DocumentOut.model_validate(await service.get(document_id))


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID, user: WriterDep, service: DocumentServiceDep, ip: ClientIpDep
) -> Response:
    """Delete a document and its chunks (owner or admin only; audited)."""
    await service.delete(user, document_id, ip)
    return Response(status_code=204)
