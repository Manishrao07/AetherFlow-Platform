"""Metrics summary: a human-readable view of the Prometheus metrics (Grafana has the detail)."""

from __future__ import annotations

import os

import streamlit as st

from ui.api_client import ApiClient, ApiError
from ui.format import money, parse_prometheus, total


def render(api: ApiClient) -> None:
    st.header("Metrics")
    _health(api)
    try:
        text = api.metrics_text(os.environ.get("METRICS_TOKEN") or None)
    except ApiError as exc:
        st.warning(f"Metrics are not available: {exc.message}")
        return
    samples = parse_prometheus(text)

    requests = total(samples, "http_requests_total")
    errors = sum(
        v
        for labels, v in samples.get("http_requests_total", [])
        if labels["status"].startswith("5")
    )
    runs = {
        status: total(samples, "workflow_runs_total", status=status)
        for status in ("COMPLETED", "FAILED", "CANCELLED")
    }
    a, b, c, d = st.columns(4)
    a.metric("HTTP requests", int(requests))
    b.metric("Server errors (5xx)", int(errors))
    c.metric("Runs completed", int(runs["COMPLETED"]))
    d.metric("Runs failed", int(runs["FAILED"]))

    left, right = st.columns(2)
    with left:
        st.subheader("LLM")
        st.metric("Tokens in", int(total(samples, "llm_tokens_total", kind="prompt")))
        st.metric("Tokens out", int(total(samples, "llm_tokens_total", kind="completion")))
        st.metric("Estimated spend", money(total(samples, "llm_cost_usd_total")))
    with right:
        st.subheader("Reliability")
        retries = int(total(samples, "workflow_step_retries_total"))
        failed_tools = int(
            sum(
                v
                for labels, v in samples.get("tool_executions_total", [])
                if labels["status"] in {"failed", "timeout"}
            )
        )
        hits = total(samples, "cache_operations_total", result="hit")
        misses = total(samples, "cache_operations_total", result="miss")
        st.metric("Step retries", retries)
        st.metric("Tool failures", failed_tools)
        st.metric("Cache hit ratio", f"{hits / (hits + misses):.0%}" if hits + misses else "n/a")

    breakers = samples.get("circuit_breaker_open", [])
    if breakers:
        st.subheader("Circuit breakers")
        names = {1.0: "open", 0.5: "probing", 0.0: "closed"}
        st.dataframe(
            [
                {"breaker": labels["breaker"], "state": names.get(value, str(value))}
                for labels, value in breakers
            ],
            hide_index=True,
        )
    st.caption(
        "Full dashboards: `docker compose --profile observability up`, then open Grafana on :3000."
    )


def _health(api: ApiClient) -> None:
    try:
        ready = api.readiness()
    except ApiError as exc:
        st.error(exc.message)
        return
    checks = ", ".join(
        f"{name}: {'ok' if ok else 'DOWN'}" for name, ok in ready.get("checks", {}).items()
    )
    (st.success if ready.get("status") == "ready" else st.error)(
        f"API {ready.get('status')} ({checks})"
    )
