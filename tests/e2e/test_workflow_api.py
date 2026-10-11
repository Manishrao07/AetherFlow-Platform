"""Workflow API contract: validation, auth, idempotency and visibility."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.e2e.helpers import as_user, flow_app, start, wait_status

GOAL = "What is the refund window for digital goods?"
URL = "/api/v1/workflows"


async def test_requests_are_validated_and_authorised(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op, viewer = await as_user(http, "operator"), await as_user(http, "viewer")
        assert (await http.post(URL, json={"goal": GOAL})).status_code == 401
        assert (await http.post(URL, json={"goal": "hi"}, headers=op)).status_code == 422
        assert (await http.post(URL, json={}, headers=op)).status_code == 422
        assert (await http.post(URL, json={"goal": GOAL}, headers=viewer)).status_code == 403
        assert (await http.get(f"{URL}/not-a-uuid", headers=op)).status_code == 422
        missing = await http.get(f"{URL}/{uuid.uuid4()}", headers=op)
        assert missing.status_code == 404 and missing.json()["error"]["code"]
        assert (await http.get(f"{URL}/{uuid.uuid4()}/timeline", headers=op)).status_code == 404
        assert (await http.post(f"{URL}/{uuid.uuid4()}/cancel", headers=op)).status_code == 404


async def test_idempotency_key_makes_creation_safe_to_repeat(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        headers = {**op, "Idempotency-Key": "retry-me-123"}
        first = await http.post(URL, json={"goal": GOAL}, headers=headers)
        second = await http.post(URL, json={"goal": GOAL}, headers=headers)
        assert first.status_code == 202 and second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        other = await http.post(
            URL, json={"goal": GOAL}, headers={**op, "Idempotency-Key": "x2-12345"}
        )
        assert other.json()["id"] != first.json()["id"]
        await wait_status(http, op, first.json()["id"], "COMPLETED")


async def test_runs_are_visible_to_owner_admin_and_approver_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        approver, admin, viewer = [await as_user(http, n) for n in ("approver", "admin", "viewer")]
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "COMPLETED")
        for headers in (op, approver, admin):
            assert (await http.get(f"{URL}/{run['id']}", headers=headers)).status_code == 200
        assert (await http.get(f"{URL}/{run['id']}", headers=viewer)).status_code == 404

        mine = (await http.get(URL, headers=op)).json()
        assert mine["total"] == 1 and mine["items"][0]["id"] == run["id"]
        assert (await http.get(URL, headers=viewer)).json()["total"] == 0
        assert (await http.get(f"{URL}?status=FAILED", headers=op)).json()["total"] == 0
        assert (await http.get(f"{URL}?limit=1&offset=5", headers=op)).json()["items"] == []
        assert (await http.get(f"{URL}?status=BOGUS", headers=op)).status_code == 422
