"""Tracing: nesting, trace ids, errors, exporters and the OTLP wire format."""

import asyncio
import json
import logging

import httpx
import pytest

from app.core.observability.tracing import (
    ConsoleExporter,
    InMemoryExporter,
    OtlpHttpExporter,
    Span,
    Tracer,
    build_tracer,
    current_span,
    parse_traceparent,
    to_otlp_payload,
)

TRACE = "0af7651916cd43dd8448eb211c80319c"
PARENT = "b7ad6b7169203331"


def test_spans_nest_and_share_a_trace() -> None:
    exporter = InMemoryExporter()
    tracer = Tracer(exporter)
    assert current_span() is None
    with tracer.span("outer", kind="a") as outer, tracer.span("inner") as inner:
        assert current_span() is inner
        inner.set(tokens=5)
    assert current_span() is None
    first, second = exporter.spans  # inner finishes (and exports) first
    assert first.name == "inner" and second.name == "outer"
    assert first.trace_id == second.trace_id == outer.trace_id
    assert first.parent_id == second.span_id and second.parent_id is None
    assert first.attributes == {"tokens": 5} and second.attributes == {"kind": "a"}
    assert len(first.trace_id) == 32 and len(first.span_id) == 16
    assert first.duration_ms >= 0 and exporter.named("outer") == [second]


def test_explicit_trace_id_starts_a_new_trace_even_inside_a_span() -> None:
    exporter = InMemoryExporter()
    tracer = Tracer(exporter)
    with tracer.span("request"), tracer.span("job", trace_id=TRACE, parent_id=PARENT):
        pass
    job = exporter.named("job")[0]
    assert job.trace_id == TRACE and job.parent_id == PARENT


def test_errors_are_recorded_and_re_raised() -> None:
    exporter = InMemoryExporter()
    tracer = Tracer(exporter)
    with pytest.raises(ValueError), tracer.span("boom"):
        raise ValueError("bad input")
    span = exporter.spans[0]
    assert span.status == "error" and span.error == "ValueError: bad input"


def test_a_failing_exporter_never_breaks_the_work() -> None:
    class Broken:
        def export(self, span: Span) -> None:
            raise RuntimeError("exporter down")

        async def shutdown(self) -> None:
            return None

    with Tracer(Broken()).span("work"):
        result = 42
    assert result == 42


def test_concurrent_tasks_do_not_share_parents() -> None:
    exporter = InMemoryExporter()
    tracer = Tracer(exporter)

    async def work(name: str) -> None:
        with tracer.span(name):
            await asyncio.sleep(0.01)
            with tracer.span(f"{name}.child"):
                await asyncio.sleep(0.01)

    async def main() -> None:
        await asyncio.gather(work("a"), work("b"))

    asyncio.run(main())
    parent_of = {s.name: s.parent_id for s in exporter.spans}
    ids = {s.name: s.span_id for s in exporter.spans}
    assert parent_of["a.child"] == ids["a"] and parent_of["b.child"] == ids["b"]


def test_console_exporter_writes_one_structured_log_line(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.INFO, logger="app.trace"),
        pytest.raises(KeyError),
        Tracer(ConsoleExporter()).span("db.query", rows=3),
    ):
        raise KeyError("x")
    record = next(r for r in caplog.records if r.name == "app.trace")
    assert record.__dict__["span_name"] == "db.query"
    assert record.__dict__["attributes"] == {"rows": 3}
    assert record.__dict__["span_status"] == "error" and "KeyError" in record.__dict__["span_error"]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (f"00-{TRACE}-{PARENT}-01", (TRACE, PARENT)),
        ("garbage", None),
        (f"01-{TRACE}-{PARENT}-01", None),
        (f"00-{'0' * 32}-{PARENT}-01", None),
        (f"00-{TRACE}-{'0' * 16}-01", None),
        (f"00-{'z' * 32}-{PARENT}-01", None),
        (f"00-{TRACE[:-1]}-{PARENT}-01", None),
    ],
)
def test_parse_traceparent(header: str, expected: tuple[str, str] | None) -> None:
    assert parse_traceparent(header) == expected


def sample_span(**kw: object) -> Span:
    base: dict[str, object] = {
        "name": "tool.calculator", "trace_id": TRACE, "span_id": PARENT, "parent_id": None,
        "start_ns": 10, "end_ns": 20,
    }  # fmt: skip
    base.update(kw)
    return Span(**base)  # type: ignore[arg-type]


def test_otlp_payload_matches_the_wire_format() -> None:
    span = sample_span(
        parent_id="1111111111111111",
        attributes={"flag": True, "n": 3, "ratio": 0.5, "name": "x"},
        status="error", error="Boom: bad",
    )  # fmt: skip
    payload = to_otlp_payload([span], "svc")
    resource = payload["resourceSpans"][0]
    assert resource["resource"]["attributes"][0]["value"] == {"stringValue": "svc"}
    out = resource["scopeSpans"][0]["spans"][0]
    assert out["traceId"] == TRACE and out["parentSpanId"] == "1111111111111111"
    assert out["startTimeUnixNano"] == "10" and out["status"] == {"code": 2, "message": "Boom: bad"}
    values = {a["key"]: a["value"] for a in out["attributes"]}
    assert values == {
        "flag": {"boolValue": True}, "n": {"intValue": "3"},
        "ratio": {"doubleValue": 0.5}, "name": {"stringValue": "x"},
    }  # fmt: skip
    assert (
        "parentSpanId"
        not in to_otlp_payload([sample_span()], "s")["resourceSpans"][0]["scopeSpans"][0]["spans"][
            0
        ]
    )


async def test_otlp_exporter_posts_batches() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    exporter = OtlpHttpExporter("http://collector/v1/traces", "svc", batch_size=2, client=client)
    for _ in range(3):
        exporter.export(sample_span())
    await exporter.shutdown()  # flushes
    assert len(bodies) == 2  # 3 spans, batches of 2
    assert exporter.dropped == 0


async def test_otlp_exporter_drops_instead_of_failing() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("collector down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    exporter = OtlpHttpExporter("http://collector/v1/traces", "svc", max_queue=2, client=client)
    for _ in range(3):
        exporter.export(sample_span())  # third one overflows the queue
    assert exporter.dropped == 1
    await exporter.flush()
    assert exporter.dropped == 3
    await exporter.shutdown()


async def test_otlp_exporter_logs_a_rejection(caplog: pytest.LogCaptureFixture) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400)))
    exporter = OtlpHttpExporter("http://collector/v1/traces", "svc", client=client)
    exporter.export(sample_span())
    with caplog.at_level(logging.WARNING, logger="app.trace"):
        await exporter.flush()
    assert any("rejected" in r.getMessage() for r in caplog.records)
    await exporter.shutdown()


def test_build_tracer_chooses_the_exporter() -> None:
    assert isinstance(build_tracer("console", service_name="s").exporter, ConsoleExporter)
    assert type(build_tracer("none", service_name="s").exporter).__name__ == "NoopExporter"
    with pytest.raises(ValueError, match="otlp_endpoint"):
        build_tracer("otlp", service_name="s")


async def test_background_flush_loop_sends_spans() -> None:
    sent = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        sent.set()
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    exporter = OtlpHttpExporter(
        "http://c/v1/traces", "svc", flush_interval_seconds=0.02, client=client
    )
    exporter.export(sample_span())
    await asyncio.wait_for(sent.wait(), 2)
    await exporter.shutdown()


def test_span_duration_survives_a_wall_clock_that_steps_backwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter([2_000_000_000, 1_000_000_000])  # the wall clock jumps back one second
    monkeypatch.setattr("app.core.observability.tracing.time.time_ns", lambda: next(ticks))
    exporter = InMemoryExporter()
    with Tracer(exporter).span("x"):
        pass
    assert exporter.spans[0].duration_ms >= 0
