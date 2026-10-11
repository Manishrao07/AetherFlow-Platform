"""Prometheus metrics for the generic building blocks (HTTP, LLM, cache, circuit breakers).

What: one process-wide registry with the metrics every service built on ``app/core`` needs, plus
``render()`` for the ``/metrics`` endpoint. Project-specific metrics (workflows, steps, tools,
approvals) live in ``app/metrics.py`` and register on the SAME registry.
Why a module-level registry: metrics are process-global by nature (one Prometheus scrape per
process) and defining them once at import time avoids "duplicated timeseries" errors when tests
build several apps in one process.
Why bounded labels: every label value creates a time series. We only use values from small fixed
sets (route TEMPLATES like ``/api/v1/workflows/{run_id}``, never raw paths or ids; model names;
status classes) so cardinality cannot grow with traffic or attackers.
Connects to: ``middleware.py`` (HTTP), ``llm/client.py`` (LLM), ``cache.py``, ``/metrics`` router.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

REGISTRY = CollectorRegistry(auto_describe=True)

# WHY these buckets: API calls are mostly fast, LLM calls take seconds.
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
_LLM_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60)

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"], registry=REGISTRY
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency (until the response is complete)",
    ["method", "route"],
    buckets=_HTTP_BUCKETS,
    registry=REGISTRY,
)
HTTP_IN_PROGRESS = Gauge(
    "http_requests_in_progress", "HTTP requests currently being handled", registry=REGISTRY
)

LLM_REQUESTS = Counter(
    "llm_requests_total",
    "LLM provider attempts (each retry counts)",
    ["provider", "model", "agent", "status"],
    registry=REGISTRY,
)
LLM_DURATION = Histogram(
    "llm_request_duration_seconds",
    "LLM provider call latency",
    ["provider", "model"],
    buckets=_LLM_BUCKETS,
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "llm_tokens_total", "Tokens used", ["provider", "model", "kind"], registry=REGISTRY
)
LLM_COST = Counter(
    "llm_cost_usd_total", "Estimated spend in USD", ["provider", "model"], registry=REGISTRY
)

CACHE_OPERATIONS = Counter(
    "cache_operations_total", "Cache lookups", ["cache", "result"], registry=REGISTRY
)


def route_label(scope: Mapping[str, Any]) -> str:
    """The matched route TEMPLATE (``/workflows/{run_id}``), or a fixed word when unmatched."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def observe_http(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_DURATION.labels(method, route).observe(seconds)


def observe_llm(
    *,
    provider: str,
    model: str,
    agent: str,
    status: str,
    seconds: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Record one provider attempt (success or failure)."""
    LLM_REQUESTS.labels(provider, model, agent, status).inc()
    LLM_DURATION.labels(provider, model).observe(seconds)
    if prompt_tokens:
        LLM_TOKENS.labels(provider, model, "prompt").inc(prompt_tokens)
    if completion_tokens:
        LLM_TOKENS.labels(provider, model, "completion").inc(completion_tokens)
    if cost_usd:
        LLM_COST.labels(provider, model).inc(cost_usd)


class _BreakerCollector(Collector):
    """Reports each registered circuit breaker's state at scrape time.

    WHY a collector instead of a Gauge set on every transition: the breaker stays free of any
    metrics code (it is generic), and the value can never be stale.
    """

    def __init__(self) -> None:
        self._sources: dict[str, Callable[[], str]] = {}

    def register(self, name: str, state: Callable[[], str]) -> None:
        self._sources[name] = state

    def collect(self) -> Iterable[GaugeMetricFamily]:
        family = GaugeMetricFamily(
            "circuit_breaker_open",
            "1 when the circuit is open (calls fail fast), 0 when closed, 0.5 when probing",
            labels=["breaker"],
        )
        for name, state in self._sources.items():
            value = {"open": 1.0, "half_open": 0.5}.get(state(), 0.0)
            family.add_metric([name], value)
        yield family


BREAKERS = _BreakerCollector()
REGISTRY.register(BREAKERS)


def render() -> tuple[bytes, str]:
    """The text exposition format and its content type, for the ``/metrics`` response."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
