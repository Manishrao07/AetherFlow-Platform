"""RBAC enforcement on real endpoints, plus the audit trail those actions leave."""

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import bearer, login


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("admin@demo.local", 200),
        ("approver@demo.local", 403),
        ("operator@demo.local", 403),
        ("viewer@demo.local", 403),
    ],
)
async def test_user_list_is_admin_only(
    seeded_client: AsyncClient, email: str, expected: int
) -> None:
    tokens = await login(seeded_client, email)
    response = await seeded_client.get("/api/v1/users", headers=bearer(tokens))
    assert response.status_code == expected
    if expected == 403:
        assert response.json()["error"]["code"] == "forbidden"


async def test_audit_log_is_admin_only(seeded_client: AsyncClient) -> None:
    assert (await seeded_client.get("/api/v1/audit-logs")).status_code == 401
    for email in ("approver@demo.local", "viewer@demo.local"):
        tokens = await login(seeded_client, email)
        assert (
            await seeded_client.get("/api/v1/audit-logs", headers=bearer(tokens))
        ).status_code == 403
    admin = await login(seeded_client, "admin@demo.local")
    assert (await seeded_client.get("/api/v1/audit-logs", headers=bearer(admin))).status_code == 200


async def test_pagination_envelope_and_limits(seeded_client: AsyncClient) -> None:
    admin = bearer(await login(seeded_client, "admin@demo.local"))
    page = (await seeded_client.get("/api/v1/users?limit=2&offset=1", headers=admin)).json()
    assert (
        page["total"] == 4
        and page["limit"] == 2
        and page["offset"] == 1
        and len(page["items"]) == 2
    )
    too_big = await seeded_client.get("/api/v1/users?limit=1000", headers=admin)
    assert too_big.status_code == 422
    assert (await seeded_client.get("/api/v1/users?offset=-1", headers=admin)).status_code == 422


async def test_role_change_takes_effect_immediately_and_is_audited(
    seeded_client: AsyncClient,
) -> None:
    admin = bearer(await login(seeded_client, "admin@demo.local"))
    viewer_tokens = await login(seeded_client, "viewer@demo.local")
    users = (await seeded_client.get("/api/v1/users", headers=admin)).json()["items"]
    viewer_id = next(u["id"] for u in users if u["email"] == "viewer@demo.local")

    assert (
        await seeded_client.get("/api/v1/users", headers=bearer(viewer_tokens))
    ).status_code == 403
    changed = await seeded_client.put(
        f"/api/v1/users/{viewer_id}/roles", headers=admin, json={"roles": ["admin", "operator"]}
    )
    assert changed.status_code == 200 and changed.json()["roles"] == ["admin", "operator"]
    # SAME old token now works as admin: roles are read from the DB, not frozen in the JWT.
    assert (
        await seeded_client.get("/api/v1/users", headers=bearer(viewer_tokens))
    ).status_code == 200

    logs = (
        await seeded_client.get(
            "/api/v1/audit-logs?action=user.roles_changed&resource_type=user", headers=admin
        )
    ).json()
    entry = next(e for e in logs["items"] if e["resource_id"] == viewer_id)
    assert entry["details"] == {"before": ["viewer"], "after": ["admin", "operator"]}
    assert entry["actor_email"] == "admin@demo.local" and entry["request_id"]


async def test_invalid_role_rejected(seeded_client: AsyncClient) -> None:
    admin = bearer(await login(seeded_client, "admin@demo.local"))
    users = (await seeded_client.get("/api/v1/users", headers=admin)).json()["items"]
    uid = users[0]["id"]
    for payload in ({"roles": ["superuser"]}, {"roles": []}):
        r = await seeded_client.put(f"/api/v1/users/{uid}/roles", headers=admin, json=payload)
        assert r.status_code == 422


async def test_admin_cannot_lock_themselves_out(seeded_client: AsyncClient) -> None:
    admin = bearer(await login(seeded_client, "admin@demo.local"))
    me = (await seeded_client.get("/api/v1/auth/me", headers=admin)).json()
    demote = await seeded_client.put(
        f"/api/v1/users/{me['id']}/roles", headers=admin, json={"roles": ["viewer"]}
    )
    disable = await seeded_client.put(
        f"/api/v1/users/{me['id']}/active", headers=admin, json={"is_active": False}
    )
    assert demote.status_code == disable.status_code == 409


async def test_unknown_user_is_404_with_envelope(seeded_client: AsyncClient) -> None:
    admin = bearer(await login(seeded_client, "admin@demo.local"))
    r = await seeded_client.get("/api/v1/users/00000000-0000-0000-0000-000000000000", headers=admin)
    assert r.status_code == 404
    error = r.json()["error"]
    assert error["code"] == "not_found" and error["request_id"] == r.headers["x-request-id"]
    assert (await seeded_client.get("/api/v1/users/not-a-uuid", headers=admin)).status_code == 422


async def test_failed_login_and_denied_access_are_audited_despite_rollback(
    seeded_client: AsyncClient,
) -> None:
    await seeded_client.post(
        "/api/v1/auth/login", json={"email": "viewer@demo.local", "password": "wrong-pass-1"}
    )
    viewer = await login(seeded_client, "viewer@demo.local")
    await seeded_client.get("/api/v1/users", headers=bearer(viewer))  # 403
    admin = bearer(await login(seeded_client, "admin@demo.local"))
    failures = (
        await seeded_client.get("/api/v1/audit-logs?action=auth.login", headers=admin)
    ).json()
    assert any(
        e["outcome"] == "failure"
        and e["actor_email"] == "viewer@demo.local"
        and e["details"] == {"reason": "bad_password"}
        for e in failures["items"]
    )
    denied = (
        await seeded_client.get("/api/v1/audit-logs?action=access.denied", headers=admin)
    ).json()
    assert any(
        e["outcome"] == "denied"
        and e["actor_email"] == "viewer@demo.local"
        and e["details"]["path"] == "/api/v1/users"
        for e in denied["items"]
    )


async def test_audit_details_never_contain_secrets(
    seeded_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    app = seeded_client.app  # type: ignore[attr-defined]
    await app.state.audit.record(
        "test.event", details={"password": "hunter2", "note": "mail me at a@b.co", "ok": 1}
    )
    async with session_factory() as s:
        details = await s.scalar(
            sa.text(
                "SELECT details FROM audit_logs WHERE action = 'test.event' "
                "ORDER BY created_at DESC LIMIT 1"
            )
        )
    assert details == {"password": "[REDACTED]", "note": "mail me at [EMAIL]", "ok": 1}


async def test_audit_write_failure_does_not_break_the_request(
    seeded_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        def __call__(self) -> None:
            raise ConnectionError("audit db down")

    seeded_client.app.state.audit._session_factory = Broken()  # type: ignore[attr-defined]
    r = await seeded_client.post(
        "/api/v1/auth/login", json={"email": "viewer@demo.local", "password": "Demo#Pass123"}
    )
    assert r.status_code == 200  # user action still succeeds; failure is logged
    assert "failed to write audit log" in caplog.text
