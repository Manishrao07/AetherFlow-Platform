"""Core metrics helpers: route labels, LLM observation and the breaker collector."""

from app.core.llm.circuit_breaker import BreakerState, CircuitBreaker
from app.core.observability.metrics import (
    BREAKERS,
    REGISTRY,
    observe_http,
    observe_llm,
    render,
    route_label,
)


class FakeRoute:
    path = "/api/v1/workflows/{run_id}"


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_route_label_uses_the_template_never_the_raw_path() -> None:
    assert route_label({"route": FakeRoute(), "path": "/api/v1/workflows/123"}) == FakeRoute.path
    assert route_label({"path": "/anything/an/attacker/invents"}) == "unmatched"


def test_http_observation_updates_counter_and_histogram() -> None:
    labels = {"method": "GET", "route": "/unit-test", "status": "200"}
    before = value("http_requests_total", **labels)
    observe_http("GET", "/unit-test", 200, 0.02)
    assert value("http_requests_total", **labels) == before + 1
    assert value("http_request_duration_seconds_count", method="GET", route="/unit-test") >= 1


def test_llm_observation_tracks_tokens_cost_and_status() -> None:
    base = {"provider": "p", "model": "m"}
    observe_llm(
        provider="p", model="m", agent="planner", status="succeeded", seconds=0.3,
        prompt_tokens=100, completion_tokens=40, cost_usd=0.002,
    )  # fmt: skip
    observe_llm(provider="p", model="m", agent="planner", status="failed", seconds=0.1)
    assert value("llm_tokens_total", kind="prompt", **base) == 100
    assert value("llm_tokens_total", kind="completion", **base) == 40
    assert abs(value("llm_cost_usd_total", **base) - 0.002) < 1e-9
    status = {"agent": "planner", **base}
    assert value("llm_requests_total", status="succeeded", **status) == 1
    assert value("llm_requests_total", status="failed", **status) == 1


def test_breaker_state_is_reported_at_scrape_time() -> None:
    breaker = CircuitBreaker("unit-test-breaker", failure_threshold=1, recovery_seconds=60)
    BREAKERS.register(breaker.name, lambda: breaker.state.value)
    assert value("circuit_breaker_open", breaker="unit-test-breaker") == 0
    breaker._state = BreakerState.OPEN
    breaker._opened_at = breaker._clock()
    assert value("circuit_breaker_open", breaker="unit-test-breaker") == 1
    breaker._opened_at -= 120  # cool-off elapsed -> probing
    assert value("circuit_breaker_open", breaker="unit-test-breaker") == 0.5


def test_render_returns_the_prometheus_text_format() -> None:
    body, content_type = render()
    assert content_type.startswith("text/plain") and b"http_requests_total" in body
