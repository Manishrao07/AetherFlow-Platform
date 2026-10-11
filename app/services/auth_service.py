"""Registration, login, token issuing, refresh rotation and logout.

What: ``AuthService`` holds all authentication rules; routers just call it.
Why a service: security rules in one testable place instead of spread over endpoints.

Key security decisions (each explainable in an interview):
* Same error for "unknown email" and "wrong password" -> no account enumeration, and a dummy
  hash check on unknown emails keeps response time similar.
* Refresh tokens ROTATE: each use revokes the old token and issues a new one.
* Reuse detection: presenting an already-used refresh token means it was copied, so ALL of
  that user's refresh tokens are revoked (forces re-login) and the event is audited.
* Self-registration only ever grants ``viewer`` (least privilege).
* Failed attempts are audited through a separate transaction so the record survives rollback.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.auth import TokenPair
from app.core.audit import AuditService
from app.core.config import Settings
from app.core.errors import AuthenticationError, ConflictError
from app.core.rbac import Principal
from app.core.security import (
    TokenClaims,
    TokenCodec,
    TokenError,
    TokenType,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.models import RefreshToken, Role, User
from app.models.enums import RoleName

logger = logging.getLogger(__name__)

# Verified against when the email is unknown so both paths cost one argon2 verification.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


class AuthService:
    def __init__(
        self, session: AsyncSession, codec: TokenCodec, settings: Settings, audit: AuditService
    ) -> None:
        self._session = session
        self._codec = codec
        self._settings = settings
        self._audit = audit

    # ------------------------------------------------------------------ users
    async def register(
        self, email: str, full_name: str, password: str, ip: str | None = None
    ) -> User:
        """Create an account with the least-privileged role."""
        viewer = await self._session.scalar(
            sa.select(Role).where(Role.name == RoleName.VIEWER.value)
        )
        if viewer is None:
            raise RuntimeError("roles not seeded - run `alembic upgrade head`")
        user = User(
            email=email, full_name=full_name, password_hash=hash_password(password), roles=[viewer]
        )
        self._session.add(user)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            await self._audit.record(
                "auth.register", actor_email=email, outcome="failure",
                details={"reason": "email_taken"}, ip_address=ip,
            )  # fmt: skip
            raise ConflictError("An account with this email already exists") from exc
        await self._audit.record(
            "auth.register", actor_id=user.id, actor_email=email, resource_type="user",
            resource_id=user.id, ip_address=ip,
        )  # fmt: skip
        return user

    async def authenticate(self, email: str, password: str, ip: str | None = None) -> User:
        """Verify credentials or raise the generic 401."""
        user = await self._session.scalar(sa.select(User).where(sa.func.lower(User.email) == email))
        stored = user.password_hash if user else _DUMMY_HASH
        valid = verify_password(password, stored)
        if user is None or not valid or not user.is_active:
            reason = "unknown_user" if user is None else "bad_password" if not valid else "inactive"
            await self._audit.record(
                "auth.login", actor_id=user.id if user else None, actor_email=email,
                outcome="failure", details={"reason": reason}, ip_address=ip,
            )  # fmt: skip
            raise AuthenticationError("Invalid email or password")
        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)  # transparent upgrade of old hashes
        await self._audit.record(
            "auth.login", actor_id=user.id, actor_email=user.email, resource_type="user",
            resource_id=user.id, ip_address=ip,
        )  # fmt: skip
        return user

    # ----------------------------------------------------------------- tokens
    async def issue_tokens(self, user: User) -> TokenPair:
        """Mint an access + refresh pair and register the refresh token."""
        access_lifetime = timedelta(minutes=self._settings.access_token_minutes)
        access, _ = self._codec.encode(user.id, TokenType.ACCESS, access_lifetime)
        refresh, claims = self._codec.encode(
            user.id, TokenType.REFRESH, timedelta(days=self._settings.refresh_token_days)
        )
        self._session.add(
            RefreshToken(id=claims.jti, user_id=user.id, expires_at=claims.expires_at)
        )
        await self._session.flush()
        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in=int(access_lifetime.total_seconds()),
        )

    async def refresh(self, refresh_token: str, ip: str | None = None) -> TokenPair:
        """Exchange a valid refresh token for a new pair (rotation + reuse detection)."""
        claims = self._decode_refresh(refresh_token)
        # WHY FOR UPDATE: two simultaneous refreshes with one token must not both succeed.
        row = await self._session.scalar(
            sa.select(RefreshToken).where(RefreshToken.id == claims.jti).with_for_update()
        )
        if row is None or row.user_id != claims.subject:
            raise AuthenticationError("Invalid refresh token")
        if row.revoked_at is not None:
            await self._revoke_all(row.user_id)
            await self._session.commit()  # WHY commit now: the 401 below would roll it back
            await self._audit.record(
                "auth.token_reuse_detected", actor_id=row.user_id, resource_type="user",
                resource_id=row.user_id, outcome="denied", ip_address=ip,
            )  # fmt: skip
            raise AuthenticationError("Refresh token was already used")
        user = await self._session.get(User, row.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Account is disabled")
        row.revoked_at = datetime.now(UTC)
        pair = await self.issue_tokens(user)
        await self._audit.record(
            "auth.refresh", actor_id=user.id, actor_email=user.email, ip_address=ip
        )
        return pair

    async def logout(self, refresh_token: str) -> None:
        """Revoke the presented refresh token (idempotent)."""
        claims = self._decode_refresh(refresh_token)
        await self._session.execute(
            sa.update(RefreshToken)
            .where(RefreshToken.id == claims.jti, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await self._audit.record("auth.logout", actor_id=claims.subject)

    async def principal_from_access_token(self, token: str) -> Principal:
        """Resolve a bearer token to a Principal with CURRENT roles from the database."""
        try:
            claims = self._codec.decode(token, TokenType.ACCESS)
        except TokenError as exc:
            raise AuthenticationError("Invalid or expired token") from exc
        user = await self._session.get(User, claims.subject)
        if user is None or not user.is_active:
            raise AuthenticationError("Invalid or expired token")
        return Principal(user.id, user.email, frozenset(role.name for role in user.roles))

    # --------------------------------------------------------------- internal
    def _decode_refresh(self, token: str) -> TokenClaims:
        try:
            return self._codec.decode(token, TokenType.REFRESH)
        except TokenError as exc:
            raise AuthenticationError("Invalid refresh token") from exc

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> None:
        await self._revoke_all(user_id)

    async def _revoke_all(self, user_id: uuid.UUID) -> None:
        await self._session.execute(
            sa.update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
