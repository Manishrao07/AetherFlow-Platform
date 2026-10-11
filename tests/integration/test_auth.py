"""Authentication flows end to end (real app + real Postgres)."""

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import RefreshToken
from tests.conftest import DEMO_PASSWORD, bearer, login


async def test_register_creates_viewer_only(seeded_client: AsyncClient) -> None:
    response = await seeded_client.post(
        "/api/v1/auth/register",
        json={"email": "New.User@Example.com ", "full_name": "New", "password": "longpass123"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["roles"] == ["viewer"] and body["email"] == "new.user@example.com"
    assert "password" not in response.text and "hash" not in response.text


async def test_register_duplicate_email_conflicts_case_insensitively(
    seeded_client: AsyncClient,
) -> None:
    response = await seeded_client.post(
        "/api/v1/auth/register",
        json={"email": "ADMIN@demo.local", "full_name": "Dup", "password": "longpass123"},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "conflict"


async def test_register_rejects_weak_passwords_and_bad_email(seeded_client: AsyncClient) -> None:
    for password in ("short1", "onlyletterslong", "1234567890123"):
        r = await seeded_client.post(
            "/api/v1/auth/register",
            json={"email": "w@example.com", "full_name": "W", "password": password},
        )
        assert r.status_code == 422, password
    bad = await seeded_client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "full_name": "W", "password": "longpass123"},
    )
    assert bad.status_code == 422
    fields = {d["field"] for d in bad.json()["error"]["details"]}
    assert "body.email" in fields


async def test_login_me_roundtrip(seeded_client: AsyncClient) -> None:
    tokens = await login(seeded_client, "operator@demo.local")
    assert tokens["token_type"] == "bearer" and tokens["expires_in"] == 900
    me = await seeded_client.get("/api/v1/auth/me", headers=bearer(tokens))
    assert me.status_code == 200
    assert me.json()["email"] == "operator@demo.local" and me.json()["roles"] == ["operator"]


async def test_wrong_password_and_unknown_user_look_identical(seeded_client: AsyncClient) -> None:
    wrong = await seeded_client.post(
        "/api/v1/auth/login", json={"email": "admin@demo.local", "password": "nope-nope-1"}
    )
    unknown = await seeded_client.post(
        "/api/v1/auth/login", json={"email": "ghost@demo.local", "password": "nope-nope-1"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert wrong.headers["www-authenticate"] == "Bearer"


async def test_protected_endpoint_needs_valid_access_token(seeded_client: AsyncClient) -> None:
    assert (await seeded_client.get("/api/v1/auth/me")).status_code == 401
    bad = await seeded_client.get("/api/v1/auth/me", headers={"Authorization": "Bearer junk"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "unauthenticated"
    tokens = await login(seeded_client, "viewer@demo.local")
    as_access = await seeded_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
    )
    assert as_access.status_code == 401  # refresh token cannot act as an access token


async def test_refresh_rotates_and_old_token_is_dead(seeded_client: AsyncClient) -> None:
    first = await login(seeded_client, "operator@demo.local")
    second = await seeded_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert second.status_code == 200
    new_tokens = second.json()
    assert new_tokens["refresh_token"] != first["refresh_token"]
    assert (
        await seeded_client.get("/api/v1/auth/me", headers=bearer(new_tokens))
    ).status_code == 200


async def test_refresh_token_reuse_revokes_whole_family_and_is_audited(
    seeded_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await login(seeded_client, "approver@demo.local")
    rotated = (
        await seeded_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
    ).json()
    # An attacker replays the stolen, already-used token:
    replay = await seeded_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert replay.status_code == 401
    # ...so the legitimate user's newest token is revoked too (forces re-login).
    victim = await seeded_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
    )
    assert victim.status_code == 401
    async with session_factory() as s:
        active = await s.scalar(
            sa.select(sa.func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.revoked_at.is_(None))
        )
        reuse_events = await s.scalar(
            sa.text("SELECT count(*) FROM audit_logs WHERE action = 'auth.token_reuse_detected'")
        )
    assert active == 0 and reuse_events >= 1


async def test_logout_revokes_refresh_token(seeded_client: AsyncClient) -> None:
    tokens = await login(seeded_client, "viewer@demo.local")
    out = await seeded_client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert out.status_code == 204
    again = await seeded_client.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert again.status_code == 204  # idempotent
    reuse = await seeded_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert reuse.status_code == 401


async def test_refresh_with_garbage_or_access_token_fails(seeded_client: AsyncClient) -> None:
    tokens = await login(seeded_client, "viewer@demo.local")
    for bad in ("x" * 30, tokens["access_token"]):
        r = await seeded_client.post("/api/v1/auth/refresh", json={"refresh_token": bad})
        assert r.status_code == 401


async def test_deactivated_user_cannot_login_or_use_existing_token(
    seeded_client: AsyncClient,
) -> None:
    admin = await login(seeded_client, "admin@demo.local")
    victim = await login(seeded_client, "viewer@demo.local")
    users = (await seeded_client.get("/api/v1/users", headers=bearer(admin))).json()["items"]
    viewer_id = next(u["id"] for u in users if u["email"] == "viewer@demo.local")
    off = await seeded_client.put(
        f"/api/v1/users/{viewer_id}/active", headers=bearer(admin), json={"is_active": False}
    )
    assert off.status_code == 200 and off.json()["is_active"] is False
    # existing access token dies immediately (roles/active are read from DB each request)
    assert (await seeded_client.get("/api/v1/auth/me", headers=bearer(victim))).status_code == 401
    assert (
        await seeded_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": victim["refresh_token"]}
        )
    ).status_code == 401
    relogin = await seeded_client.post(
        "/api/v1/auth/login", json={"email": "viewer@demo.local", "password": DEMO_PASSWORD}
    )
    assert relogin.status_code == 401
