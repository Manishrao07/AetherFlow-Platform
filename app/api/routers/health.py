"""Liveness and readiness probes.

What: ``/healthz`` says "the process is up"; ``/readyz`` says "dependencies are usable".
Why two probes: orchestrators restart a container when liveness fails but only stop routing
traffic to it when readiness fails. A temporarily unreachable DB must not cause restarts.
Connects to: docker-compose health checks and (later) Kubernetes probes.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.db.session import check_db

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Always 200 while the event loop is responsive. Does no I/O on purpose."""
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe (database + LLM provider)")
async def readyz(request: Request) -> JSONResponse:
    """200 if every dependency answers, otherwise 503 with per-check detail."""
    # WHY no real LLM call: probes run every few seconds and a paid API call each time would
    # cost money; "provider configured and circuit not open" is the useful readiness signal.
    checks = {
        "database": await check_db(request.app.state.engine),
        "llm": request.app.state.llm.is_healthy(),
    }
    ready = all(checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )
