"""Application factory and lifespan.

What: ``create_app()`` builds the FastAPI app: validated settings, JSON logging, DB engine
with startup retry, middleware and routers.
Why a factory (not a module-level ``app``): tests can build an app with custom settings, and
importing this module has no side effects. Run with ``uvicorn app.main:create_app --factory``.
Connects to: everything; later phases add routers, security middleware and the worker here.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import (
    approvals,
    auth,
    conversations,
    documents,
    health,
    memory,
    metrics,
    tools,
    users,
    workflows,
)
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.observability.middleware import RequestContextMiddleware
from app.core.ratelimit import build_rate_limiter
from app.core.security import TokenCodec
from app.core.security_middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from app.runtime import build_runtime

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Invalid configuration raises here, before serving anything."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = await build_runtime(settings)
        app.state.runtime = runtime
        app.state.engine = runtime.engine
        app.state.session_factory = runtime.session_factory
        app.state.audit = runtime.audit
        app.state.llm = runtime.llm
        app.state.tool_registry = runtime.registry
        app.state.tool_executor = runtime.executor
        app.state.agents = runtime.agents
        app.state.run_store = runtime.store
        app.state.queue = runtime.queue
        app.state.worker = runtime.worker
        app.state.knowledge = runtime.knowledge
        app.state.cache = runtime.cache
        # WHY optional: API replicas can run with WORKER_ENABLED=false and let dedicated worker
        # processes (python -m app.orchestrator.runner) do the execution.
        await runtime.start(run_worker=settings.worker_enabled)
        logger.info("application started", extra={"environment": settings.environment.value})
        try:
            yield
        finally:
            # WHY: graceful shutdown - release pooled connections cleanly.
            await runtime.close()
            logger.info("application stopped")

    app = FastAPI(
        title="Multi-Agent Workflow Platform",
        version="0.1.0",
        description="Planner, Retrieval, Execution and Review agents with approvals and tracing.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.token_codec = TokenCodec(
        settings.jwt_secret.get_secret_value(), settings.jwt_algorithm
    )
    install_error_handlers(app)

    # WHY this order: Starlette runs the LAST added middleware first (outermost). The request
    # ID must be outermost so every rejection below (413/429) still carries a request ID.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    if settings.rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            limiter=build_rate_limiter(settings.redis_url),
            default_per_minute=settings.rate_limit_default_per_minute,
            auth_per_minute=settings.rate_limit_auth_per_minute,
            trust_proxy_headers=settings.trust_proxy_headers,
        )
    cors_origins = list(settings.cors_origins)
    for dev_origin in (
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:8501",
        "http://localhost:8585",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ):
        if dev_origin not in cors_origins:
            cors_origins.append(dev_origin)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=r"^https:\/\/.*\.vercel\.app$",
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(tools.router)
    app.include_router(conversations.router)
    app.include_router(documents.router)
    app.include_router(memory.router)
    app.include_router(workflows.router)
    app.include_router(approvals.router)
    return app
