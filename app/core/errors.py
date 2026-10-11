"""One error shape for the whole API, plus the handlers that produce it.

What: ``AppError`` and subclasses (each carries an HTTP status and a stable machine code) and
``install_error_handlers`` which maps every failure - ours, FastAPI validation, framework
HTTP errors and unexpected crashes - to ``{"error": {code, message, details, request_id}}``.
Why: clients (and the Streamlit UI) get one predictable format; the ``request_id`` links an
error response to the exact log lines. Unexpected errors return a generic message so
internals (stack traces, SQL) never leak.
Connects to: request-ID middleware (supplies ``request_id``); every router raises AppError.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for expected, client-visible errors."""

    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.headers = headers


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthenticated"

    def __init__(self, message: str = "Authentication required") -> None:
        # WHY header: RFC 7235 requires it on 401 responses.
        super().__init__(message, headers={"WWW-Authenticate": "Bearer"})


class PermissionDeniedError(AppError):
    status_code = 403
    code = "forbidden"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


def error_body(
    code: str, message: str, request_id: str | None, details: Any = None
) -> dict[str, Any]:
    """Build the standard envelope."""
    return {
        "error": {"code": code, "message": message, "details": details, "request_id": request_id}
    }


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers so every failure uses the envelope."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, _request_id(request), exc.details),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # WHY drop 'input'/'ctx': echoing submitted values back could reflect secrets.
        details = [
            {"field": ".".join(str(part) for part in err["loc"]), "message": err["msg"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body(
                "validation_error", "Request validation failed", _request_id(request), details
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, str(exc.detail), _request_id(request)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", "Internal server error", _request_id(request)),
        )
