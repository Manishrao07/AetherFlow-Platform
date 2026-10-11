"""Password hashing basics."""

from app.core.security import hash_password, password_needs_rehash, verify_password


def test_hash_verify_roundtrip() -> None:
    hashed = hash_password("S3cret!pass")
    assert hashed != "S3cret!pass" and hashed.startswith("$argon2id$")
    assert verify_password("S3cret!pass", hashed)
    assert not verify_password("wrong", hashed)


def test_same_password_gets_different_salts() -> None:
    assert hash_password("same") != hash_password("same")


def test_garbage_hash_is_a_mismatch_not_an_error() -> None:
    assert not verify_password("x", "not-a-hash")


def test_fresh_hash_does_not_need_rehash() -> None:
    assert not password_needs_rehash(hash_password("x"))
