"""Prometheus scrape endpoint.

What: ``GET /metrics`` returns every metric in the Prometheus text format.
Why unauthenticated by default: Prometheus scrapes inside the private compose network, and the
metrics contain no user data (labels are route templates, tool names, statuses - never ids).
If the port is exposed, set ``METRICS_TOKEN`` and Prometheus must send it as a bearer token.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.core.errors import AuthenticationError
from app.core.observability.metrics import render

router = APIRouter(tags=["observability"])


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    token = request.app.state.settings.metrics_token
    if token is not None and token.get_secret_value():
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        # WHY compare_digest: constant-time comparison, no timing side channel.
        if not hmac.compare_digest(supplied, token.get_secret_value()):
            raise AuthenticationError("Missing or invalid metrics token")
    body, content_type = render()
    return Response(content=body, media_type=content_type)
