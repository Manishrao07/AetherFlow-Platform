"""Redaction must catch secrets by key name and by value shape."""

from app.core.redaction import REDACTED, redact, redact_text


def test_sensitive_keys_are_masked_recursively() -> None:
    data = {"user": "bob", "password": "hunter2", "nested": {"X-Api-Key": "abc", "ok": 1}}
    assert redact(data) == {
        "user": "bob",
        "password": REDACTED,
        "nested": {"X-Api-Key": REDACTED, "ok": 1},
    }


def test_lists_are_walked() -> None:
    assert redact([{"token": "t"}, "plain"]) == [{"token": REDACTED}, "plain"]


def test_value_patterns() -> None:
    text = "mail bob@example.com with Bearer abc.def-123 and key sk-ant-abcdefghijklmnop1234"
    out = redact_text(text)
    assert "bob@example.com" not in out and "[EMAIL]" in out
    assert "abc.def-123" not in out
    assert "sk-ant" not in out and "[API_KEY]" in out


def test_jwt_and_card_numbers() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
    assert redact_text(f"t={jwt}") == "t=[JWT]"
    assert redact_text("card 4111 1111 1111 1111 ok") == "card [CARD_NUMBER] ok"


def test_non_string_scalars_untouched() -> None:
    assert redact(42) == 42
    assert redact(None) is None


def test_token_counts_are_not_secrets_but_credentials_still_are() -> None:
    data = {
        "prompt_tokens": 12, "completion_tokens": 7, "max_tokens": 1024, "token_count": 3,
        "access_token": "a", "refresh_token": "r", "api_token": "x", "Token": "y",
        "tokens_secret": "z",
    }  # fmt: skip
    out = redact(data)
    assert out["prompt_tokens"] == 12 and out["completion_tokens"] == 7
    assert out["max_tokens"] == 1024 and out["token_count"] == 3
    for key in ("access_token", "refresh_token", "api_token", "Token", "tokens_secret"):
        assert out[key] == REDACTED, key
