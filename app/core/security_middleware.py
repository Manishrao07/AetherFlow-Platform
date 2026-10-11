"""HTTP hardening middleware: security headers, request size limit, rate limiting.

What: three small pure-ASGI middlewares.
Why: defence in depth at the edge - browsers get safe headers, oversized bodies are cut off
before they exhaust memory, and abusive clients are throttled before they reach business code.
Why pure ASGI (not BaseHTTPMiddleware): streams correctly (SSE later) and has less overhead.
Connects to: installed in ``app/main.py`` after the request-ID middleware, so error
responses carry the request ID.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import PayloadTooLargeError, RateLimitedError, error_body
from app.core.ratelimit import RateLimiter

_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware:
    """Adds standard security headers to every HTTP response."""

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self._hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_docs = scope["path"].startswith(_DOC_PATHS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                    b"cache-control": b"no-store",
                }
                # WHY skip CSP on docs: Swagger UI loads its own scripts; the JSON API itself
                # never needs to render anything, so it gets the strictest policy.
                if not is_docs:
                    extra[b"content-security-policy"] = (
                        b"default-src 'none'; frame-ancestors 'none'"
                    )
                if self._hsts:
                    extra[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains"
                present = {name.lower() for name, _ in headers}
                headers.extend((k, v) for k, v in extra.items() if k not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLargeError(Exception):
    pass


class BodySizeLimitMiddleware:
    """Rejects bodies over ``max_bytes`` (declared via Content-Length OR actually streamed)."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max:
            await self._reject(scope, receive, send)
            return

        received = 0
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                # WHY count real bytes: Content-Length can be absent (chunked) or a lie.
                if received > self._max:
                    raise _BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLargeError:
            if response_started:
                raise
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        error = PayloadTooLargeError(f"Request body exceeds {self._max} bytes")
        response = JSONResponse(
            status_code=error.status_code,
            content=error_body(error.code, error.message, scope.get("state", {}).get("request_id")),
        )
        await response(scope, receive, send)


class RateLimitMiddleware:
    """Per-client-IP throttling with a stricter bucket for credential endpoints."""

    EXEMPT_PREFIXES = ("/healthz", "/readyz", "/metrics")
    AUTH_PATHS = ("/api/v1/auth/login", "/api/v1/auth/register", "/api/v1/auth/refresh")

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RateLimiter,
        default_per_minute: int,
        auth_per_minute: int,
        trust_proxy_headers: bool = False,
    ) -> None:
        self.app = app
        self._limiter = limiter
        self._default = default_per_minute
        self._auth = auth_per_minute
        self._trust_proxy = trust_proxy_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope["method"] == "OPTIONS"
            or path.startswith(self.EXEMPT_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return

        bucket, limit = ("auth", self._auth) if path in self.AUTH_PATHS else ("api", self._default)
        result = await self._limiter.hit(
            f"{bucket}:{client_ip(scope, self._trust_proxy)}", limit, 60
        )
        if not result.allowed:
            error = RateLimitedError("Too many requests, slow down")
            response = JSONResponse(
                status_code=error.status_code,
                content=error_body(
                    error.code, error.message, scope.get("state", {}).get("request_id")
                ),
                headers={"Retry-After": str(result.retry_after_seconds)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def client_ip(scope: Scope, trust_proxy_headers: bool) -> str:
    """Best-effort client address. Only honours X-Forwarded-For when explicitly trusted."""
    if trust_proxy_headers:
        forwarded = Request(scope).headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"
