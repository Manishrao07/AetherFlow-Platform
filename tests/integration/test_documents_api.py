"""Knowledge-base endpoints: ingest (idempotent), list, search, delete rules, audit."""

import uuid

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditLog, DocumentChunk
from tests.conftest import bearer, login

BASE = "/api/v1/documents"
TEXT = (
    "Employees travelling for work may book economy class on flights shorter than six hours.\n\n"
    "Hotel stays are capped at 200 USD per night unless a manager approves an exception."
)


async def headers(http: AsyncClient, who: str) -> dict[str, str]:
    return bearer(await login(http, f"{who}@demo.local"))


async def test_ingest_search_with_citations(seeded_client: AsyncClient) -> None:
    h = await headers(seeded_client, "operator")
    created = await seeded_client.post(
        BASE, json={"title": "Travel Policy", "content": TEXT}, headers=h
    )
    assert created.status_code == 201
    body = created.json()
    assert body["created"] is True and body["document"]["chunk_count"] == 1
    assert body["document"]["status"] == "READY"
    hits = (await seeded_client.get(f"{BASE}/search?q=hotel nightly cap", headers=h)).json()
    assert hits[0]["title"] == "Travel Policy" and hits[0]["citation"] == "Travel Policy #1"
    assert "200 USD" in hits[0]["content"]
    assert (await seeded_client.get(f"{BASE}/search?q=zzzqqq", headers=h)).json() == []


async def test_reupload_is_idempotent(
    seeded_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    h = await headers(seeded_client, "operator")
    first = await seeded_client.post(BASE, json={"title": "A", "content": TEXT}, headers=h)
    again = await seeded_client.post(BASE, json={"title": "Renamed", "content": TEXT}, headers=h)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["created"] is False
    assert again.json()["document"]["id"] == first.json()["document"]["id"]
    async with session_factory() as s:
        chunks = await s.scalar(
            sa.select(sa.func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == uuid.UUID(first.json()["document"]["id"]))
        )
    assert chunks == 1


async def test_long_document_is_chunked(seeded_client: AsyncClient) -> None:
    h = await headers(seeded_client, "operator")
    content = "\n\n".join(f"Paragraph {i}. " + "lorem ipsum dolor " * 30 for i in range(12))
    body = (
        await seeded_client.post(BASE, json={"title": "Long", "content": content}, headers=h)
    ).json()
    assert body["document"]["chunk_count"] > 3


async def test_empty_or_nul_only_content_is_rejected(seeded_client: AsyncClient) -> None:
    h = await headers(seeded_client, "operator")
    response = await seeded_client.post(
        BASE, json={"title": "Blank", "content": " \x00 \n"}, headers=h
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "empty_document"
    ok = await seeded_client.post(
        BASE, json={"title": "Nul", "content": "has\x00nul bytes ok"}, headers=h
    )
    assert ok.status_code == 201


async def test_list_get_and_pagination(seeded_client: AsyncClient) -> None:
    h = await headers(seeded_client, "viewer")
    listing = (await seeded_client.get(f"{BASE}?limit=2", headers=h)).json()
    assert listing["total"] == 3 and len(listing["items"]) == 2  # three seeded documents
    first = listing["items"][0]
    assert (await seeded_client.get(f"{BASE}/{first['id']}", headers=h)).json()["title"] == first[
        "title"
    ]
    assert (await seeded_client.get(f"{BASE}/{uuid.uuid4()}", headers=h)).status_code == 404


async def test_viewer_cannot_write(seeded_client: AsyncClient) -> None:
    viewer = await headers(seeded_client, "viewer")
    response = await seeded_client.post(BASE, json={"title": "x", "content": "y"}, headers=viewer)
    assert response.status_code == 403
    doc = (await seeded_client.get(BASE, headers=viewer)).json()["items"][0]["id"]
    assert (await seeded_client.delete(f"{BASE}/{doc}", headers=viewer)).status_code == 403


async def test_only_owner_or_admin_can_delete_and_it_is_audited(
    seeded_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    owner = await headers(seeded_client, "operator")
    other = await headers(seeded_client, "approver")
    admin = await headers(seeded_client, "admin")
    doc_id = (
        await seeded_client.post(BASE, json={"title": "Mine", "content": TEXT}, headers=owner)
    ).json()["document"]["id"]
    denied = await seeded_client.delete(f"{BASE}/{doc_id}", headers=other)
    assert denied.status_code == 403
    assert (await seeded_client.delete(f"{BASE}/{doc_id}", headers=owner)).status_code == 204
    assert (await seeded_client.get(f"{BASE}/{doc_id}", headers=owner)).status_code == 404
    assert (await seeded_client.delete(f"{BASE}/{doc_id}", headers=owner)).status_code == 404
    # chunks are gone with the document
    assert (await seeded_client.get(f"{BASE}/search?q=hotel", headers=owner)).json() == []
    # admin may delete someone else's document
    seeded = (await seeded_client.get(BASE, headers=admin)).json()["items"][0]["id"]
    assert (await seeded_client.delete(f"{BASE}/{seeded}", headers=admin)).status_code == 204
    async with session_factory() as s:
        rows = (
            await s.execute(
                sa.select(AuditLog.action, AuditLog.resource_id, AuditLog.actor_email).where(
                    AuditLog.resource_id.in_([doc_id, seeded])
                )
            )
        ).all()
    # audit_logs is append-only and survives test resets, so look at THESE documents only
    assert sorted((r[0], r[1], r[2]) for r in rows) == sorted(
        [
            ("document.ingested", doc_id, "operator@demo.local"),
            ("document.deleted", doc_id, "operator@demo.local"),
            ("document.deleted", seeded, "admin@demo.local"),
        ]
    )


async def test_body_validation(seeded_client: AsyncClient) -> None:
    h = await headers(seeded_client, "operator")
    assert (
        await seeded_client.post(BASE, json={"title": "", "content": "x"}, headers=h)
    ).status_code == 422
    assert (await seeded_client.post(BASE, json={"title": "t"}, headers=h)).status_code == 422
    assert (await seeded_client.get(f"{BASE}/search", headers=h)).status_code == 422
    assert (await seeded_client.get(BASE)).status_code == 401
