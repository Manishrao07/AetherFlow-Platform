"""Metrics and traces observed from the outside: /metrics, spans per run, approval timing."""

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.observability.metrics import REGISTRY
from app.core.observability.tracing import InMemoryExporter, Tracer, get_tracer, set_tracer
from tests.e2e.helpers import as_user, flow_app, make_flaky, start, wait_status
from tests.e2e.test_approvals import GOAL as APPROVAL_GOAL
from tests.e2e.test_approvals import pending

GOAL = "What is the refund window for digital goods?"


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@asynccontextmanager
async def traced_app(
    session_factory: async_sessionmaker[AsyncSession], **overrides: object
) -> AsyncIterator[tuple[AsyncClient, InMemoryExporter]]:
    """An app whose spans are captured in memory (the tracer is process-wide, so restore it)."""
    async with flow_app(session_factory, **overrides) as http:
        previous = get_tracer()
        exporter = InMemoryExporter()
        set_tracer(Tracer(exporter))
        try:
            yield http, exporter
        finally:
            set_tracer(previous)


def counter_delta(read: Callable[[], float]) -> Callable[[], float]:
    start_value = read()
    return lambda: read() - start_value


async def test_metrics_endpoint_labels_requests_by_route_template(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "COMPLETED")
        await http.get("/healthz")
        await http.get("/definitely/not/a/route")
        body = (await http.get("/metrics")).text
        assert (await http.get("/metrics")).headers["content-type"].startswith("text/plain")
        assert 'route="/healthz"' in body
        assert 'route="/api/v1/workflows/{run_id}"' in body
        assert run["id"] not in body  # ids never become label values
        assert 'route="unmatched"' in body and "/definitely/not/a/route" not in body
        assert 'route="/metrics"' not in body  # scrapes don't measure themselves
        assert "http_request_duration_seconds_bucket" in body


async def test_metrics_can_require_a_bearer_token(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, metrics_token="scrape-token-123") as http:
        assert (await http.get("/metrics")).status_code == 401
        wrong = await http.get("/metrics", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        ok = await http.get("/metrics", headers={"Authorization": "Bearer scrape-token-123"})
        assert ok.status_code == 200 and "http_requests_total" in ok.text


async def test_a_completed_run_moves_the_workflow_llm_and_tool_metrics(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runs = counter_delta(lambda: value("workflow_runs_total", status="COMPLETED"))
    created = counter_delta(lambda: value("workflows_created_total"))
    steps = counter_delta(
        lambda: value(
            "workflow_steps_total",
            agent="retrieval",
            tool="search_knowledge_base",
            status="succeeded",
        )
    )
    tools = counter_delta(
        lambda: value("tool_executions_total", tool="search_knowledge_base", status="succeeded")
    )
    durations = counter_delta(lambda: value("workflow_duration_seconds_count", status="COMPLETED"))
    tokens = counter_delta(
        lambda: sum(
            s.value
            for f in REGISTRY.collect()
            if f.name == "llm_tokens"
            for s in f.samples
            if s.name == "llm_tokens_total"
        )
    )
    llm_calls = counter_delta(
        lambda: value(
            "llm_requests_total",
            provider="mock",
            model="mock-model",
            agent="planner",
            status="succeeded",
        )
    )
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "COMPLETED")
    assert (runs(), created(), steps(), tools(), durations(), llm_calls()) == (1, 1, 1, 1, 1, 1)
    assert tokens() > 0


async def test_retries_and_tool_failures_are_counted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    retries = counter_delta(lambda: value("workflow_step_retries_total", tool="calculator"))
    failed = counter_delta(
        lambda: value("tool_executions_total", tool="calculator", status="failed")
    )
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        make_flaky(http.app.state.tool_registry.get("calculator"), fail_times=2)  # type: ignore[attr-defined]
        run = await start(http, op, "Calculate 2 + 3 for the report")
        await wait_status(http, op, run["id"], "COMPLETED")
    assert retries() == 2 and failed() == 2


async def test_approval_wait_time_and_decisions_are_recorded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    decisions = counter_delta(lambda: value("approval_decisions_total", decision="approved"))
    waits = counter_delta(lambda: value("approval_wait_seconds_count", decision="approved"))
    created = counter_delta(lambda: value("workflows_created_total"))
    async with flow_app(session_factory) as http:
        op, approver = await as_user(http, "operator"), await as_user(http, "approver")
        run = await start(http, op, APPROVAL_GOAL)
        await wait_status(http, op, run["id"], "AWAITING_APPROVAL")
        approval_id = (await pending(http, approver))[0]["id"]
        await http.post(f"/api/v1/approvals/{approval_id}/approve", json={}, headers=approver)
        await wait_status(http, op, run["id"], "COMPLETED")
    assert decisions() == 1 and waits() == 1 and created() == 1


async def test_expired_approvals_are_counted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    expired = counter_delta(lambda: value("approval_decisions_total", decision="expired"))
    async with flow_app(session_factory, approval_ttl_seconds=1) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, APPROVAL_GOAL)
        await wait_status(http, op, run["id"], "AWAITING_APPROVAL")
        await asyncio.sleep(1.2)
        await http.app.state.worker.sweep()  # type: ignore[attr-defined]
        await wait_status(http, op, run["id"], "FAILED")
    assert expired() == 1


async def test_a_run_is_one_trace_with_agent_tool_and_llm_spans(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with traced_app(session_factory) as (http, exporter):
        op = await as_user(http, "operator")
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "COMPLETED")
        # WHY wait for the root span: COMPLETED is committed a moment before the span closes. On a
        # busy machine, leaving the app now cancelled it mid-flight and it was exported as an error.
        for _ in range(100):
            if exporter.named("workflow.run"):
                break
            await asyncio.sleep(0.05)

    trace_id = run["id"].replace("-", "")
    in_trace = [s for s in exporter.spans if s.trace_id == trace_id]
    names = {s.name for s in in_trace}
    assert {
        "workflow.run",
        "agent.planner",
        "agent.retrieval",
        "agent.review",
        "step.run",
        "tool.search_knowledge_base",
        "llm.plan",
        "llm.retrieve_answer",
        "llm.review",
    } <= names
    by_id = {s.span_id: s for s in in_trace}
    root = exporter.named("workflow.run")[0]
    assert root.parent_id is None and root.attributes["run_id"] == run["id"]
    plan_llm = exporter.named("llm.plan")[0]
    assert by_id[plan_llm.parent_id or ""].name == "agent.planner"  # llm call is inside the planner
    tool_span = exporter.named("tool.search_knowledge_base")[0]
    chain = []
    node = tool_span
    while node.parent_id:
        node = by_id[node.parent_id]
        chain.append(node.name)
    assert chain == ["agent.retrieval", "step.run", "workflow.run"]
    bad = [
        (s.name, s.status, s.duration_ms) for s in in_trace if s.duration_ms < 0 or s.status != "ok"
    ]
    assert not bad, bad


async def test_failed_steps_mark_their_spans_as_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with traced_app(session_factory, default_max_revisions=0) as (http, exporter):
        op = await as_user(http, "operator")
        run = await start(http, op, "Calculate 1 / 0 for the report")
        await wait_status(http, op, run["id"], "FAILED")
    failed = [s for s in exporter.named("tool.calculator") if s.status == "error"]
    assert (failed and "ToolError" in (failed[0].error or "")) or failed[0].error


async def test_http_requests_join_the_callers_trace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    trace_id, parent_id = "0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331"
    async with traced_app(session_factory) as (http, exporter):
        await http.get("/healthz", headers={"traceparent": f"00-{trace_id}-{parent_id}-01"})
        await http.get("/healthz", headers={"traceparent": "garbage"})
    inherited, fresh = exporter.named("http.request")[-2:]
    assert inherited.trace_id == trace_id and inherited.parent_id == parent_id
    assert fresh.trace_id != trace_id and fresh.parent_id is None
    assert inherited.attributes["route"] == "/healthz" and inherited.attributes["status"] == 200
    assert re.fullmatch(r"[0-9a-f]{32}", fresh.trace_id)
