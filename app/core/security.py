"""Password hashing and JWT access/refresh tokens.

What: argon2id password hashing, and ``TokenCodec`` which signs and verifies JWTs.
Why argon2id: the OWASP-recommended password hash (memory-hard, so GPU cracking is costly).
Why JWT: the API stays stateless - any replica can verify a token without a shared session
store. Refresh tokens are additionally tracked in the DB (see auth_service) so they can be
rotated and revoked, which pure JWT cannot do on its own.
Connects to: ``services/auth_service.py`` (issues/verifies), ``api/deps.py`` (reads bearer).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# WHY module-level instance: PasswordHasher is thread-safe and holds only parameters.
_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """Return an argon2id hash (salt and parameters are embedded in the string)."""
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time check of a password against a stored hash; never raises on mismatch."""
    try:
        return _hasher.verify(hashed, plain)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(hashed: str) -> bool:
    """True when stored hash uses outdated parameters (upgrade it at next login)."""
    return _hasher.check_needs_rehash(hashed)


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class TokenError(Exception):
    """Token is malformed, expired, wrongly signed or of the wrong type."""


@dataclass(frozen=True)
class TokenClaims:
    subject: uuid.UUID
    token_type: TokenType
    jti: uuid.UUID
    expires_at: datetime


class TokenCodec:
    """Creates and verifies JWTs with one secret and one pinned algorithm."""

    def __init__(self, secret: str, algorithm: str, issuer: str = "workflow-platform") -> None:
        self._secret = secret
        # WHY pinned: accepting the token's own "alg" header enables classic downgrade attacks.
        self._algorithm = algorithm
        self._issuer = issuer

    def encode(
        self, subject: uuid.UUID, token_type: TokenType, lifetime: timedelta
    ) -> tuple[str, TokenClaims]:
        """Return ``(jwt_string, claims)``. ``jti`` uniquely identifies this token."""
        issued = datetime.now(UTC)
        claims = TokenClaims(subject, token_type, uuid.uuid4(), issued + lifetime)
        payload = {
            "sub": str(subject),
            "typ": token_type.value,
            "jti": str(claims.jti),
            "iss": self._issuer,
            "iat": int(issued.timestamp()),
            "exp": int(claims.expires_at.timestamp()),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm), claims

    def decode(self, token: str, expected_type: TokenType) -> TokenClaims:
        """Verify signature, expiry, issuer and type; raise ``TokenError`` otherwise."""
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                options={"require": ["sub", "typ", "jti", "exp", "iss"]},
            )
            token_type = TokenType(payload["typ"])
            claims = TokenClaims(
                uuid.UUID(payload["sub"]),
                token_type,
                uuid.UUID(payload["jti"]),
                datetime.fromtimestamp(payload["exp"], tz=UTC),
            )
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise TokenError("invalid token") from exc
        # WHY: a refresh token must never be accepted as an access token (and vice versa).
        if claims.token_type is not expected_type:
            raise TokenError("wrong token type")
        return claims
