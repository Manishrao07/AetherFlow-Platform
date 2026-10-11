"""Structured JSON logging with request/run/step/user correlation IDs.

What: configures the root logger to emit one JSON object per line, automatically adding
whichever correlation IDs are bound in the current async context.
Why: JSON logs are searchable in any log platform, and correlation IDs let you follow one
request or one workflow run across API, orchestrator, agents and tools.
Connects to: the request middleware binds ``request_id``; the orchestrator binds
``run_id``/``step_id``; auth binds ``user_id``. All logs pass through ``redact``.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.core.redaction import redact

# WHY ContextVar (not a global dict): each asyncio task gets its own copy, so concurrent
# requests / concurrent workflow steps never see each other's IDs.
_log_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})  # noqa: B039

# Attributes that exist on every LogRecord; anything else came from ``extra=`` and is
# included in the JSON output.
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def get_log_context() -> dict[str, Any]:
    """Return a copy of the correlation IDs bound in the current context."""
    return dict(_log_context.get())


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind correlation IDs (request_id, run_id, ...) for the duration of a ``with`` block."""
    token = _log_context.set({**_log_context.get(), **fields})
    try:
        yield
    finally:
        _log_context.reset(token)


def bind_log_context(**fields: Any) -> None:
    """Bind fields for the rest of the current task (used by middleware / auth deps)."""
    _log_context.set({**_log_context.get(), **fields})


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON, redacting secrets and PII."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_log_context.get(),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}
        payload.update(extras)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str: never let an exotic object (UUID, datetime) crash logging.
        return json.dumps(redact(payload), default=str)


class ContextTextFormatter(logging.Formatter):
    """Human-readable formatter for local debugging (LOG_JSON=false)."""

    def format(self, record: logging.LogRecord) -> str:
        context = " ".join(f"{k}={v}" for k, v in _log_context.get().items())
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: "
        text = base + str(redact(record.getMessage())) + (f" [{context}]" if context else "")
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    """Install the root handler. Safe to call more than once (replaces old handlers)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else ContextTextFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # WHY: uvicorn's own access log would duplicate our request-completed log line.
    logging.getLogger("uvicorn.access").disabled = True
