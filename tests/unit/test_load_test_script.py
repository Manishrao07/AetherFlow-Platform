"""The load-test script's helpers (the script itself needs a live server)."""

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location(
    "load_test", Path(__file__).resolve().parents[2] / "scripts" / "load_test.py"
)
assert SPEC and SPEC.loader
load_test = importlib.util.module_from_spec(SPEC)
sys.modules["load_test"] = load_test  # dataclasses look the module up by name
SPEC.loader.exec_module(load_test)


@pytest.mark.parametrize(
    ("pct", "expected"), [(50, 5.0), (95, 10.0), (99, 10.0), (10, 1.0), (0, 1.0)]
)
def test_percentile_is_nearest_rank(pct: float, expected: float) -> None:
    assert load_test.percentile([float(n) for n in range(1, 11)], pct) == expected
    assert load_test.percentile([], 95) == 0.0


def test_phase_summary_reports_throughput_and_latency() -> None:
    phase = load_test.Phase("demo", requests=100, errors=2, seconds=4.0, latencies_ms=[10, 20, 30])
    summary = phase.summary()
    assert summary["requests_per_second"] == 25.0 and summary["errors"] == 2
    assert summary["latency_ms"] == {"p50": 20.0, "p95": 30.0, "p99": 30.0, "max": 30.0}
    assert load_test.Phase("empty").summary()["requests_per_second"] == 0


async def test_metric_total_sums_matching_samples() -> None:
    text = (
        "# HELP cache_operations_total x\n"
        'cache_operations_total{cache="retrieval",result="hit"} 7.0\n'
        'cache_operations_total{cache="retrieval",result="miss"} 3.0\n'
        'cache_operations_total{cache="llm",result="hit"} 100.0\n'
        'cache_operations_created{cache="retrieval",result="hit"} 1.7e9\n'
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=text))
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
        hit = await load_test.metric_total(
            client, "cache_operations_total", cache="retrieval", result="hit"
        )
        both = await load_test.metric_total(client, "cache_operations_total", cache="retrieval")
    assert hit == 7.0 and both == 10.0


async def test_metric_total_is_zero_when_metrics_are_protected() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(401))
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
        assert await load_test.metric_total(client, "anything") == 0.0
