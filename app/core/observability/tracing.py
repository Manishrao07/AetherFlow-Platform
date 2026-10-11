"""Lightweight distributed tracing: spans with a console exporter and an OTLP/HTTP exporter.

What: ``span("name", key=value)`` times a block of work, nests under the current span (via a
ContextVar, so concurrent asyncio tasks each see their own parent) and hands the finished span
to an exporter. Exporters: ``none``, ``console`` (one JSON log line per span) and ``otlp``
(batches spans and POSTs them as OTLP/HTTP JSON to any OpenTelemetry collector, Jaeger, Tempo...).
Why not the OpenTelemetry SDK: the platform needs three things (nested timings, a stable
trace id per workflow run, and an export path). A ~200-line module that I can explain end to end
beats a large dependency tree; the OTLP/JSON wire format is standard, so collectors don't care.
Trace identity: the orchestrator uses the RUN ID as the trace id, so every span of a run -
across API restarts, workers and revisions - lands in one trace you can find from the timeline.
Failure policy: tracing must never break a request. Export errors are logged and dropped.
Connects to: HTTP middleware (root span per request), orchestrator (run/step/agent spans),
LLM client and tool executor (leaf spans).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger("app.trace")


@dataclass
class Span:
    name: str
    trace_id: str  # 32 hex chars
    span_id: str  # 16 hex chars
    parent_id: str | None
    start_ns: int
    end_ns: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # ok | error
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        return round((self.end_ns - self.start_ns) / 1_000_000, 3)

    def set(self, **attributes: Any) -> None:
        """Attach more attributes while the span is open (e.g. token counts after a call)."""
        self.attributes.update(attributes)


class SpanExporter(Protocol):
    def export(self, span: Span) -> None: ...

    async def shutdown(self) -> None: ...


class NoopExporter:
    def export(self, span: Span) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class ConsoleExporter:
    """Writes each finished span as one structured log line (logger ``app.trace``)."""

    def export(self, span: Span) -> None:
        logger.info(
            "span",
            extra={
                "span_name": span.name,
                "trace_id": span.trace_id,
                "span_id": span.span_id,
                "parent_id": span.parent_id,
                "duration_ms": span.duration_ms,
                "span_status": span.status,
                "attributes": span.attributes,
                **({"span_error": span.error} if span.error else {}),
            },
        )

    async def shutdown(self) -> None:
        return None


class InMemoryExporter:
    """Keeps spans in a list; used by tests and handy for debugging."""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    def export(self, span: Span) -> None:
        self.spans.append(span)

    async def shutdown(self) -> None:
        return None

    def named(self, name: str) -> list[Span]:
        return [s for s in self.spans if s.name == name]


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def to_otlp_payload(spans: list[Span], service_name: str) -> dict[str, Any]:
    """Build the OTLP/HTTP JSON body (``ExportTraceServiceRequest``) for ``spans``."""
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "app.core.observability.tracing"},
                        "spans": [
                            {
                                "traceId": s.trace_id,
                                "spanId": s.span_id,
                                **({"parentSpanId": s.parent_id} if s.parent_id else {}),
                                "name": s.name,
                                "kind": 1,  # SPAN_KIND_INTERNAL
                                "startTimeUnixNano": str(s.start_ns),
                                "endTimeUnixNano": str(s.end_ns),
                                "attributes": [
                                    {"key": k, "value": _otlp_value(v)}
                                    for k, v in s.attributes.items()
                                ],
                                "status": {
                                    "code": 2 if s.status == "error" else 1,
                                    **({"message": s.error} if s.error else {}),
                                },
                            }
                            for s in spans
                        ],
                    }
                ],
            }
        ]
    }


class OtlpHttpExporter:
    """Batches spans and posts them to an OTLP/HTTP collector in the background.

    WHY a bounded queue that DROPS when full: if the collector is down we would rather lose
    traces than grow memory without limit or slow down real requests.
    """

    def __init__(
        self,
        endpoint: str,
        service_name: str,
        *,
        timeout_seconds: float = 5.0,
        flush_interval_seconds: float = 2.0,
        batch_size: int = 100,
        max_queue: int = 2000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._service = service_name
        self._interval = flush_interval_seconds
        self._batch_size = batch_size
        self._queue: asyncio.Queue[Span] = asyncio.Queue(maxsize=max_queue)
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0

    def export(self, span: Span) -> None:
        if self._task is None:
            with contextlib.suppress(RuntimeError):  # no running loop (e.g. sync tests)
                self._task = asyncio.get_running_loop().create_task(self._run())
        try:
            self._queue.put_nowait(span)
        except asyncio.QueueFull:
            self.dropped += 1

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.flush()

    async def flush(self) -> None:
        """Send everything queued so far (in batches). Never raises."""
        while not self._queue.empty():
            batch: list[Span] = []
            while len(batch) < self._batch_size and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                response = await self._client.post(
                    self._endpoint, json=to_otlp_payload(batch, self._service)
                )
                if response.status_code >= 400:
                    logger.warning("otlp export rejected", extra={"status": response.status_code})
            except httpx.HTTPError as exc:
                self.dropped += len(batch)
                logger.warning("otlp export failed", extra={"error": type(exc).__name__})

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.flush()
        await self._client.aclose()


_current: ContextVar[Span | None] = ContextVar("current_span", default=None)


class Tracer:
    def __init__(self, exporter: SpanExporter | None = None) -> None:
        self.exporter: SpanExporter = exporter or NoopExporter()

    @contextmanager
    def span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent_id: str | None = None,
        **attributes: Any,
    ) -> Iterator[Span]:
        """Time a block. Nests under the current span unless ``trace_id`` starts a new trace."""
        parent = _current.get()
        if trace_id is None and parent is not None:
            trace_id, parent_id = parent.trace_id, parent.span_id
        span = Span(
            name=name,
            trace_id=trace_id or secrets.token_hex(16),
            span_id=secrets.token_hex(8),
            parent_id=parent_id,
            start_ns=time.time_ns(),
            attributes=dict(attributes),
        )
        # WHY a monotonic clock for the duration: the wall clock can step backwards (NTP, VM
        # clock sync), which produced negative durations. start_ns stays wall-clock for export.
        started = time.monotonic_ns()
        token = _current.set(span)
        try:
            yield span
        except BaseException as exc:
            span.status = "error"
            span.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            raise
        finally:
            _current.reset(token)
            span.end_ns = span.start_ns + (time.monotonic_ns() - started)
            try:
                self.exporter.export(span)
            except Exception:  # tracing must never break the work it observes
                logger.exception("span export failed")


_tracer = Tracer()


def get_tracer() -> Tracer:
    return _tracer


def set_tracer(tracer: Tracer) -> None:
    """Install the process-wide tracer (done once at startup)."""
    global _tracer
    _tracer = tracer


def span(name: str, **attributes: Any) -> contextlib.AbstractContextManager[Span]:
    """Open a span on the process-wide tracer."""
    return _tracer.span(name, **attributes)


def current_span() -> Span | None:
    return _current.get()


def parse_traceparent(header: str) -> tuple[str, str] | None:
    """Read a W3C ``traceparent`` header -> (trace_id, parent_span_id), or None if invalid."""
    parts = header.strip().split("-")
    if len(parts) != 4 or parts[0] != "00":
        return None
    trace_id, parent_id = parts[1], parts[2]
    valid = len(trace_id) == 32 and len(parent_id) == 16
    try:
        int(trace_id, 16)
        int(parent_id, 16)
    except ValueError:
        return None
    if not valid or set(trace_id) == {"0"} or set(parent_id) == {"0"}:
        return None
    return trace_id, parent_id


def build_tracer(
    exporter: str,
    *,
    service_name: str,
    otlp_endpoint: str | None = None,
    otlp_timeout_seconds: float = 5.0,
) -> Tracer:
    """Create a tracer for ``none`` / ``console`` / ``otlp`` (the ``TRACING_EXPORTER`` values)."""
    if exporter == "console":
        return Tracer(ConsoleExporter())
    if exporter == "otlp":
        if not otlp_endpoint:
            raise ValueError("otlp_endpoint is required for the otlp exporter")
        return Tracer(
            OtlpHttpExporter(otlp_endpoint, service_name, timeout_seconds=otlp_timeout_seconds)
        )
    return Tracer(NoopExporter())
