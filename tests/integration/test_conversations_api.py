"""Conversation endpoints: ownership, history, archive, pagination, context window."""

import uuid

from httpx import AsyncClient

from tests.conftest import bearer, login

BASE = "/api/v1/conversations"


async def headers(http: AsyncClient, who: str) -> dict[str, str]:
    return bearer(await login(http, f"{who}@demo.local"))


async def new_conversation(http: AsyncClient, h: dict[str, str], title: str = "Chat") -> str:
    response = await http.post(BASE, json={"title": title}, headers=h)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def test_create_append_and_read_history(seeded_client: AsyncClient) -> None:
    h = await headers(seeded_client, "operator")
    cid = await new_conversation(seeded_client, h, "  Refund questions ")
    for text in ("first", "second", "third"):
        response = await seeded_client.post(
            f"{BASE}/{cid}/messages", json={"content": text}, headers=h
        )
        assert response.status_code == 201
        assert response.json()["role"] == "user" and response.json()["token_count"] >= 1
    history = (await seeded_client.get(f"{BASE}/{cid}/messages", headers=h)).json()
    assert [m["content"] for m in history["items"]] == ["first", "second", "third"]
    assert history["total"] == 3
    page = (await seeded_client.get(f"{BASE}/{cid}/messages?limit=2&offset=2", headers=h)).json()
    assert [m["content"] for m in page["items"]] == ["third"]
    got = (await seeded_client.get(f"{BASE}/{cid}", headers=h)).json()
    assert got["title"] == "Refund questions" and got["status"] == "ACTIVE"


async def test_list_is_private_ordered_by_activity_and_filterable(
    seeded_client: AsyncClient,
) -> None:
    op = await headers(seeded_client, "operator")
    other = await headers(seeded_client, "approver")
    older = await new_conversation(seeded_client, op, "older")
    newer = await new_conversation(seeded_client, op, "newer")
    await new_conversation(seeded_client, other, "not yours")
    # activity in the older conversation makes it the most recent
    await seeded_client.post(f"{BASE}/{older}/messages", json={"content": "hi"}, headers=op)
    items = (await seeded_client.get(BASE, headers=op)).json()["items"]
    assert [c["id"] for c in items] == [older, newer]
    await seeded_client.post(f"{BASE}/{newer}/archive", headers=op)
    active = (await seeded_client.get(f"{BASE}?status=ACTIVE", headers=op)).json()
    archived = (await seeded_client.get(f"{BASE}?status=ARCHIVED", headers=op)).json()
    assert [c["id"] for c in active["items"]] == [older]
    assert [c["id"] for c in archived["items"]] == [newer] and archived["total"] == 1


async def test_other_users_conversations_look_nonexistent(seeded_client: AsyncClient) -> None:
    owner = await headers(seeded_client, "operator")
    intruder = await headers(seeded_client, "admin")  # even admins cannot read private chats
    cid = await new_conversation(seeded_client, owner)
    for method, path, body in [
        ("get", f"{BASE}/{cid}", None),
        ("get", f"{BASE}/{cid}/messages", None),
        ("post", f"{BASE}/{cid}/messages", {"content": "x"}),
        ("post", f"{BASE}/{cid}/archive", None),
        ("post", f"{BASE}/{cid}/context", None),
    ]:
        response = await seeded_client.request(method, path, json=body, headers=intruder)
        assert response.status_code == 404, (method, path)
        assert response.json()["error"]["code"] == "not_found"
    assert (await seeded_client.get(f"{BASE}/{uuid.uuid4()}", headers=owner)).status_code == 404


async def test_archived_conversation_rejects_messages(seeded_client: AsyncClient) -> None:
    h = await headers(seeded_client, "operator")
    cid = await new_conversation(seeded_client, h)
    archived = await seeded_client.post(f"{BASE}/{cid}/archive", headers=h)
    assert archived.json()["status"] == "ARCHIVED"
    response = await seeded_client.post(
        f"{BASE}/{cid}/messages", json={"content": "late"}, headers=h
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "conflict"
    # reading history still works
    assert (await seeded_client.get(f"{BASE}/{cid}/messages", headers=h)).status_code == 200


async def test_viewer_can_read_but_not_write(seeded_client: AsyncClient) -> None:
    viewer = await headers(seeded_client, "viewer")
    assert (await seeded_client.get(BASE, headers=viewer)).status_code == 200
    assert (await seeded_client.post(BASE, json={"title": "x"}, headers=viewer)).status_code == 403


async def test_requires_authentication_and_valid_bodies(seeded_client: AsyncClient) -> None:
    assert (await seeded_client.get(BASE)).status_code == 401
    h = await headers(seeded_client, "operator")
    assert (await seeded_client.post(BASE, json={"title": ""}, headers=h)).status_code == 422
    cid = await new_conversation(seeded_client, h)
    too_long = {"content": "x" * 8001}
    assert (
        await seeded_client.post(f"{BASE}/{cid}/messages", json=too_long, headers=h)
    ).status_code == 422
    assert (
        await seeded_client.post(f"{BASE}/{cid}/messages", json={"content": ""}, headers=h)
    ).status_code == 422
    assert (await seeded_client.get(f"{BASE}/not-a-uuid", headers=h)).status_code == 422


async def test_context_endpoint_summarises_long_history(seeded_client: AsyncClient) -> None:
    h = await headers(seeded_client, "operator")
    cid = await new_conversation(seeded_client, h)
    for i in range(14):
        body = {"content": f"message {i:02d} " + "lorem ipsum " * 60}
        await seeded_client.post(f"{BASE}/{cid}/messages", json=body, headers=h)
    first = (await seeded_client.post(f"{BASE}/{cid}/context", headers=h)).json()
    assert first["newly_summarized"] > 0 and first["summary"]
    assert first["messages"][-1]["content"].startswith("message 13")
    assert first["token_estimate"] <= 2000 + 100
    second = (await seeded_client.post(f"{BASE}/{cid}/context", headers=h)).json()
    assert second["newly_summarized"] == 0 and second["summary"] == first["summary"]
    # a viewer may not trigger summarisation
    viewer = await headers(seeded_client, "viewer")
    assert (await seeded_client.post(f"{BASE}/{cid}/context", headers=viewer)).status_code == 403
