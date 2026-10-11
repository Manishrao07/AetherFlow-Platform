"""Deep research end to end: topic -> sub-questions -> parallel researchers -> report -> review."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.e2e.helpers import (
    as_user,
    flow_app,
    start,
    status_path,
    step_by_key,
    timeline,
    wait_status,
)

TOPIC = "Deep research on the refund policy for digital goods"
# WHY a dead local port: Wikipedia/arXiv fail fast and offline, exercising the "one source is
# down" path while the knowledge base still answers.
OFFLINE = {
    "wikipedia_base_url": "http://127.0.0.1:9",
    "arxiv_base_url": "http://127.0.0.1:9",
    "research_timeout_seconds": 2.0,
}


async def test_deep_run_produces_a_cited_report(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, **OFFLINE) as http:
        op = await as_user(http, "operator")
        created = await start(http, op, TOPIC, mode="deep")
        assert created["mode"] == "deep"
        run = await wait_status(http, op, created["id"], "COMPLETED", timeout=40)
        assert run["review_verdict"] == "APPROVED"

        agents = [s["agent"] for s in run["plan"]["steps"]]
        assert agents == ["research"] * 4 + ["synthesizer"]
        summary = run["result"]["summary"]
        assert summary.startswith("# Research report:")
        assert "## Sources" in summary and "Refund Policy" in summary

        tl = await timeline(http, op, created["id"])
        assert status_path(tl) == ["PLANNING", "EXECUTING", "REVIEWING", "COMPLETED"]
        first = step_by_key(tl, "s1")
        assert first["status"] == "SUCCEEDED" and first["output"]["grounded"] is True
        assert any("wikipedia_search" in f for f in first["output"]["sources_failed"])
        report = step_by_key(tl, "s5")["output"]
        assert report["coverage"] > 0 and report["sources"]
        assert {"planner", "research", "synthesizer", "review"} <= {
            c["agent"] for c in tl["llm_calls"]
        }
        assert "search_knowledge_base" in {t["tool_name"] for t in tl["tool_executions"]}


async def test_default_mode_is_still_standard(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        created = await start(http, op, "What is the refund window for digital goods?")
        assert created["mode"] == "standard"
        run = await wait_status(http, op, created["id"], "COMPLETED")
        assert {s["agent"] for s in run["plan"]["steps"]} == {"retrieval"}
