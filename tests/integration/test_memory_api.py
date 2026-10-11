"""Long-term memory endpoints: privacy, upsert, search, deletion rights, audit."""

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditLog, User
from tests.conftest import bearer, login

BASE = "/api/v1/memory"


async def headers(http: AsyncClient, who: str) -> dict[str, str]:
    return bearer(await login(http, f"{who}@demo.local"))


async def test_upsert_overwrites_same_key(seeded_client: AsyncClient) -> None:
    h = await headers(seeded_client, "operator")
    body = {"content": "Prefers weekly summaries", "kind": "PREFERENCE", "importance": 3}
    first = await seeded_client.put(f"{BASE}/report_frequency", json=body, headers=h)
    assert first.status_code == 200 and first.json()["kind"] == "PREFERENCE"
    second = await seeded_client.put(
        f"{BASE}/report_frequency", json={"content": "Prefers daily summaries"}, headers=h
    )
    assert (
        second.json()["content"] == "Prefers daily summaries" and second.json()["importance"] == 1
    )
    listing = (await seeded_client.get(BASE, headers=h)).json()
    assert listing["total"] == 1 and listing["items"][0]["key"] == "report_frequency"


async def test_search_ranks_relevant_memory_first(seeded_client: AsyncClient) -> None:
    h = await headers(seeded_client, "operator")
    await seeded_client.put(f"{BASE}/diet", json={"content": "Vegetarian lunch options"}, headers=h)
    await seeded_client.put(
        f"{BASE}/reports", json={"content": "Wants weekly sales reports"}, headers=h
    )
    found = (await seeded_client.get(f"{BASE}/search?q=weekly report", headers=h)).json()
    assert [m["key"] for m in found] == ["reports"]
    default = (await seeded_client.get(f"{BASE}/search", headers=h)).json()
    assert len(default) == 2


async def test_memory_is_private_per_user(seeded_client: AsyncClient) -> None:
    alice = await headers(seeded_client, "operator")
    bob = await headers(seeded_client, "approver")
    await seeded_client.put(
        f"{BASE}/secret", json={"content": "Alice's private note"}, headers=alice
    )
    assert (await seeded_client.get(BASE, headers=bob)).json()["total"] == 0
    assert (await seeded_client.get(f"{BASE}/search?q=private", headers=bob)).json() == []
    assert (await seeded_client.delete(f"{BASE}/secret", headers=bob)).status_code == 404
    assert (await seeded_client.get(BASE, headers=alice)).json()["total"] == 1


async def test_delete_one_and_erase_all_are_audited(
    seeded_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    h = await headers(seeded_client, "operator")
    for key in ("a", "b", "c"):
        await seeded_client.put(f"{BASE}/{key}", json={"content": f"fact {key}"}, headers=h)
    assert (await seeded_client.delete(f"{BASE}/a", headers=h)).status_code == 204
    assert (await seeded_client.delete(f"{BASE}/a", headers=h)).status_code == 404
    erased = await seeded_client.delete(BASE, headers=h)
    assert erased.json() == {"deleted": 2}
    assert (await seeded_client.get(BASE, headers=h)).json()["total"] == 0
    async with session_factory() as s:
        actions = (
            await s.scalars(
                sa.select(AuditLog.action)
                .join(User, User.id == AuditLog.actor_id)
                # audit_logs survives test resets; this user row is new in every test
                .where(AuditLog.action.like("memory.%"), User.email == "operator@demo.local")
                .order_by(AuditLog.created_at)
            )
        ).all()
    assert actions == ["memory.deleted", "memory.erased"]


async def test_viewer_can_read_and_erase_own_data_but_not_write(seeded_client: AsyncClient) -> None:
    viewer = await headers(seeded_client, "viewer")
    put = await seeded_client.put(f"{BASE}/k", json={"content": "x"}, headers=viewer)
    assert put.status_code == 403
    assert (await seeded_client.get(BASE, headers=viewer)).status_code == 200
    assert (await seeded_client.delete(BASE, headers=viewer)).json() == {"deleted": 0}


async def test_validation(seeded_client: AsyncClient) -> None:
    h = await headers(seeded_client, "operator")
    assert (
        await seeded_client.put(f"{BASE}/k", json={"content": ""}, headers=h)
    ).status_code == 422
    bad_importance = {"content": "x", "importance": 9}
    assert (await seeded_client.put(f"{BASE}/k", json=bad_importance, headers=h)).status_code == 422
    assert (
        await seeded_client.put(f"{BASE}/{'k' * 121}", json={"content": "x"}, headers=h)
    ).status_code == 422
    assert (await seeded_client.get(BASE)).status_code == 401
