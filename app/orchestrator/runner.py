"""Standalone worker process: ``python -m app.orchestrator.runner``.

What: runs only the orchestrator (no HTTP). It recovers and drives runs found in Postgres.
Why: shows the scaling path - API replicas set ``WORKER_ENABLED=false`` and any number of these
processes do the heavy lifting. No broker is needed because the database holds the work and
leases stop two workers from driving the same run.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from prometheus_client import start_http_server

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.observability.metrics import REGISTRY
from app.runtime import build_runtime

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    runtime = await build_runtime(settings)
    if settings.worker_metrics_port:
        # WHY: a worker has no HTTP server, so expose its metrics on a small dedicated port.
        start_http_server(settings.worker_metrics_port, registry=REGISTRY)
    await runtime.start(run_worker=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("standalone worker running")
    await stop.wait()
    await runtime.close()


if __name__ == "__main__":  # pragma: no cover  (process entry point)
    asyncio.run(run_worker())
