"""JSON logs carry correlation IDs and never leak secrets."""

import json
import logging

from app.core.logging import (
    ContextTextFormatter,
    JsonFormatter,
    bind_log_context,
    configure_logging,
    get_log_context,
    log_context,
)


def _record(message: str, **extra: object) -> logging.LogRecord:
    logger = logging.getLogger("test")
    return logger.makeRecord("test", logging.INFO, "f.py", 1, message, (), None, extra=extra)


def test_json_output_includes_context_and_extras() -> None:
    with log_context(request_id="req-1", run_id="run-9"):
        line = JsonFormatter().format(_record("hello", duration_ms=5))
    payload = json.loads(line)
    assert payload["message"] == "hello"
    assert payload["request_id"] == "req-1" and payload["run_id"] == "run-9"
    assert payload["duration_ms"] == 5 and payload["level"] == "INFO"


def test_context_is_restored_after_block() -> None:
    with log_context(request_id="a"):
        assert get_log_context() == {"request_id": "a"}
    assert "request_id" not in get_log_context()


def test_secrets_are_redacted_in_json_logs() -> None:
    line = JsonFormatter().format(_record("login", password="hunter2", note="mail a@b.co"))
    assert "hunter2" not in line and "a@b.co" not in line


def test_exception_is_included() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.getLogger("t").makeRecord(
            "t", logging.ERROR, "f.py", 1, "failed", (), sys.exc_info()
        )
    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exception"]


def test_text_formatter_and_configure() -> None:
    bind_log_context(user_id="u1")
    text = ContextTextFormatter().format(_record("hi"))
    assert "hi" in text and "user_id=u1" in text
    configure_logging("DEBUG", json_logs=False)
    configure_logging("INFO", json_logs=True)
    assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)
