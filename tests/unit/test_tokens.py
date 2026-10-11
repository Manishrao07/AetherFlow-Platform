"""JWT codec: valid round-trip and every way a token can be wrong."""

import uuid
from datetime import timedelta

import jwt
import pytest

from app.core.security import TokenCodec, TokenError, TokenType

SECRET = "unit-test-secret-unit-test-secret-1234"
codec = TokenCodec(SECRET, "HS256")


def test_roundtrip() -> None:
    user = uuid.uuid4()
    token, issued = codec.encode(user, TokenType.ACCESS, timedelta(minutes=5))
    claims = codec.decode(token, TokenType.ACCESS)
    assert claims.subject == user and claims.jti == issued.jti
    assert claims.token_type is TokenType.ACCESS


def test_expired_token_rejected() -> None:
    token, _ = codec.encode(uuid.uuid4(), TokenType.ACCESS, timedelta(seconds=-10))
    with pytest.raises(TokenError):
        codec.decode(token, TokenType.ACCESS)


def test_wrong_type_rejected_both_ways() -> None:
    access, _ = codec.encode(uuid.uuid4(), TokenType.ACCESS, timedelta(minutes=5))
    refresh, _ = codec.encode(uuid.uuid4(), TokenType.REFRESH, timedelta(minutes=5))
    with pytest.raises(TokenError, match="wrong token type"):
        codec.decode(access, TokenType.REFRESH)
    with pytest.raises(TokenError, match="wrong token type"):
        codec.decode(refresh, TokenType.ACCESS)


def test_wrong_secret_and_tampering_rejected() -> None:
    token, _ = codec.encode(uuid.uuid4(), TokenType.ACCESS, timedelta(minutes=5))
    with pytest.raises(TokenError):
        TokenCodec("another-secret-another-secret-12345", "HS256").decode(token, TokenType.ACCESS)
    head, body, sig = token.split(".")
    with pytest.raises(TokenError):
        codec.decode(f"{head}.{body}x.{sig}", TokenType.ACCESS)


def test_alg_none_token_rejected() -> None:
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "typ": "access",
            "jti": str(uuid.uuid4()),
            "iss": "workflow-platform",
            "exp": 9999999999,
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(TokenError):
        codec.decode(forged, TokenType.ACCESS)


def test_wrong_issuer_and_missing_claims_rejected() -> None:
    other = jwt.encode({"sub": "x", "exp": 9999999999}, SECRET, algorithm="HS256")
    with pytest.raises(TokenError):
        codec.decode(other, TokenType.ACCESS)
    wrong_iss = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "typ": "access",
            "jti": str(uuid.uuid4()),
            "iss": "evil",
            "exp": 9999999999,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        codec.decode(wrong_iss, TokenType.ACCESS)


def test_garbage_rejected() -> None:
    with pytest.raises(TokenError):
        codec.decode("not-a-jwt", TokenType.ACCESS)
