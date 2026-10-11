"""Request middleware: correlation ID, access log, HTTP metrics and a root trace span.

What: reads ``X-Request-ID`` (or generates one), binds it to the logging context, echoes it
on the response and logs one structured line per request with status and duration. It also
records the Prometheus request count/latency (labelled by route TEMPLATE) and opens a root
tracing span, continuing the caller's W3C ``traceparent`` when one is sent.
Why: when a user reports a problem, the request ID in the error body leads straight to
every log line for that request.
Connects to: ``core/logging.py`` (context binding); error envelopes (Phase 2) include it.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import log_context
from app.core.observability.metrics import HTTP_IN_PROGRESS, observe_http, route_label
from app.core.observability.tracing import get_tracer, parse_traceparent

logger = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"
# WHY not measured: Prometheus scraping itself would otherwise dominate the numbers.
_UNMETERED_PATHS = frozenset({"/metrics"})
# WHY validate: the ID is echoed into headers and logs, so never trust arbitrary input
# (prevents header/log injection).
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")


class RequestContextMiddleware:
    """Pure ASGI middleware (cheaper and streaming-safe vs BaseHTTPMiddleware, so SSE works)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER.lower().encode(), b"").decode()
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status_code = 500

        async def send_with_header(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
                message["headers"] = headers
            await send(message)

        headers = dict(scope["headers"])
        inherited = parse_traceparent(headers.get(b"traceparent", b"").decode())
        metered = scope["path"] not in _UNMETERED_PATHS
        if metered:
            HTTP_IN_PROGRESS.inc()
        with (
            get_tracer().span(
                "http.request",
                trace_id=inherited[0] if inherited else None,
                parent_id=inherited[1] if inherited else None,
                method=scope["method"],
                request_id=request_id,
            ) as root,
            log_context(request_id=request_id, trace_id=root.trace_id),
        ):
            try:
                await self.app(scope, receive, send_with_header)
            finally:
                seconds = time.perf_counter() - started
                route = route_label(scope)
                root.set(route=route, status=status_code)
                if metered:
                    HTTP_IN_PROGRESS.dec()
                    observe_http(scope["method"], route, status_code, seconds)
                logger.info(
                    "request completed",
                    extra={
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": status_code,
                        "duration_ms": round(seconds * 1000, 2),
                    },
                )
