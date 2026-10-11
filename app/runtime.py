"""Runtime: builds every long-lived object once, for the API process and for standalone workers.

What: ``build_runtime(settings)`` connects to the database (with startup retry), creates the LLM
client, tool registry/executor, agents, orchestrator engine and worker, and returns them in one
``Runtime``; ``Runtime.close`` shuts them down in the right order.
Why a separate module: ``app.main`` (API) and ``app.orchestrator.runner`` (worker-only process)
need identical wiring. One builder means they cannot drift apart - the basis of the "scale
workers independently of the API" story.
"""

from __future__ import annotations

import logging
import socket
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents import AgentSet, build_agents
from app.agents.base import AgentDeps
from app.core.audit import AuditService
from app.core.cache import Cache, build_cache
from app.core.config import Settings
from app.core.db.session import build_engine, build_session_factory, wait_for_db
from app.core.embeddings import build_embedder
from app.core.llm.client import LLMClient, build_llm_client
from app.core.observability.tracing import Tracer, build_tracer, set_tracer
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.store import RunStore
from app.orchestrator.worker import InProcessWorker, JobQueue, NullQueue
from app.services.documents import backfill_embeddings
from app.services.knowledge import KnowledgeSearch
from app.services.llm_usage import DbUsageRecorder
from app.services.synonyms import expand_query
from app.tools.builtin import build_tool_registry
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    audit: AuditService
    llm: LLMClient
    registry: ToolRegistry
    executor: ToolExecutor
    agents: AgentSet
    store: RunStore
    workflow_engine: WorkflowEngine
    worker: InProcessWorker
    queue: JobQueue
    cache: Cache
    knowledge: KnowledgeSearch
    tracer: Tracer

    async def start(self, *, run_worker: bool) -> None:
        if self.knowledge.embedder is not None:
            count = await backfill_embeddings(self.session_factory, self.knowledge.embedder)
            if count:
                logger.info("embedded existing chunks", extra={"chunks": count})
                await self.knowledge.invalidate()
        if run_worker:
            await self.worker.start()

    async def close(self) -> None:
        """Graceful shutdown: stop workers first (they release leases), then the pool."""
        await self.worker.stop()
        await self.tracer.exporter.shutdown()  # flush buffered spans
        await self.cache.close()
        await self.engine.dispose()


async def build_runtime(settings: Settings) -> Runtime:
    engine = build_engine(settings)
    # WHY: Postgres may still be starting (docker compose); wait instead of crashing.
    await wait_for_db(engine, settings.db_connect_retries, settings.db_connect_retry_delay_seconds)
    session_factory = build_session_factory(engine)
    audit = AuditService(session_factory)
    tracer = build_tracer(
        settings.tracing_exporter.value,
        service_name=settings.tracing_service_name,
        otlp_endpoint=settings.otlp_endpoint,
        otlp_timeout_seconds=settings.otlp_timeout_seconds,
    )
    set_tracer(tracer)
    # WHY one shared cache: retrieval and LLM entries use different key prefixes; with REDIS_URL
    # every API/worker replica sees the same entries and the same invalidation counter.
    cache = build_cache(settings.redis_url, settings.cache_max_entries)
    llm_cache = cache if settings.cache_enabled else None
    llm = build_llm_client(settings, DbUsageRecorder(session_factory), llm_cache)
    embedder = build_embedder(settings, expand=expand_query)
    knowledge = KnowledgeSearch(
        llm_cache, settings.retrieval_cache_ttl_seconds, embedder,
        settings.semantic_min_similarity, settings.semantic_max_candidates,
    )  # fmt: skip
    registry = build_tool_registry(settings, knowledge=knowledge)
    executor = ToolExecutor(registry, session_factory, audit)
    agents = build_agents(
        AgentDeps(
            llm=llm,
            registry=registry,
            executor=executor,
            session_factory=session_factory,
            settings=settings,
        )
    )
    store = RunStore(session_factory)
    # WHY unique per process: the lease owner must identify THIS worker instance.
    owner = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    workflow_engine = WorkflowEngine(
        settings=settings, session_factory=session_factory, store=store, agents=agents,
        llm=llm, owner=owner,
    )  # fmt: skip
    worker = InProcessWorker(workflow_engine, store, session_factory, settings)
    queue: JobQueue = worker if settings.worker_enabled else NullQueue()
    return Runtime(
        engine, session_factory, audit, llm, registry, executor, agents, store,
        workflow_engine, worker, queue, cache, knowledge, tracer,
    )  # fmt: skip
