"""Authentication endpoints: register, login, refresh, logout, me."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AuthServiceDep, ClientIpDep, PrincipalDep, SessionDep
from app.api.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from app.models import User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, auth: AuthServiceDep, ip: ClientIpDep) -> UserOut:
    """Create an account. New accounts get the read-only `viewer` role; an admin can promote."""
    user = await auth.register(body.email, body.full_name, body.password, ip)
    return UserOut.from_user(user)


@router.post("/login", response_model=TokenPair)
async def login(body: LoginRequest, auth: AuthServiceDep, ip: ClientIpDep) -> TokenPair:
    """Exchange email + password for an access token and a refresh token."""
    user = await auth.authenticate(body.email, body.password, ip)
    return await auth.issue_tokens(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest, auth: AuthServiceDep, ip: ClientIpDep) -> TokenPair:
    """Rotate a refresh token. Each refresh token works exactly once."""
    return await auth.refresh(body.refresh_token, ip)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, auth: AuthServiceDep) -> None:
    """Revoke a refresh token (idempotent)."""
    await auth.logout(body.refresh_token)


@router.get("/me", response_model=UserOut)
async def me(principal: PrincipalDep, session: SessionDep) -> UserOut:
    """Return the authenticated caller's profile."""
    user = await session.get(User, principal.user_id)
    assert user is not None  # principal was just resolved from this row
    return UserOut.from_user(user)
