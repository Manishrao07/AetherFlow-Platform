"""Report export, follow-up questions, confidence and "similar past research" over the API."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.e2e.helpers import as_user, flow_app, start, timeline, wait_status

OFFLINE = {
    "wikipedia_base_url": "http://127.0.0.1:9",
    "arxiv_base_url": "http://127.0.0.1:9",
    "research_timeout_seconds": 2.0,
}
TOPIC = "Deep research on the refund policy for digital goods"


async def finished_run(http: AsyncClient, headers: dict[str, str], goal: str = TOPIC) -> str:
    created = await start(http, headers, goal, mode="deep")
    await wait_status(http, headers, created["id"], "COMPLETED", timeout=40)
    return str(created["id"])


async def test_report_downloads_and_confidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, **OFFLINE) as http:
        op = await as_user(http, "operator")
        run_id = await finished_run(http, op)

        run = (await http.get(f"/api/v1/workflows/{run_id}", headers=op)).json()
        confidence = run["result"]["confidence"]
        assert confidence["label"] in {"high", "medium", "low"} and 0 < confidence["score"] <= 1

        md = await http.get(f"/api/v1/workflows/{run_id}/report", headers=op)
        assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
        assert "*Confidence:" in md.text and "## Sources" in md.text

        pdf = await http.get(f"/api/v1/workflows/{run_id}/report?format=pdf", headers=op)
        assert pdf.content.startswith(b"%PDF")
        assert pdf.headers["content-disposition"].startswith("attachment")


async def test_follow_up_question_uses_the_report_and_is_tracked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, **OFFLINE) as http:
        op = await as_user(http, "operator")
        run_id = await finished_run(http, op)
        answer = await http.post(
            f"/api/v1/workflows/{run_id}/ask",
            json={"question": "When are digital goods refundable?"},
            headers=op,
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert "licence key" in body["answer"] and body["citations"]
        tl = await timeline(http, op, run_id)
        assert sum(c["agent"] == "synthesizer" for c in tl["llm_calls"]) == 2  # report + follow-up


async def test_report_and_follow_up_need_a_finished_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, **OFFLINE) as http:
        op = await as_user(http, "operator")
        missing = "00000000-0000-0000-0000-000000000000"
        assert (
            await http.get(f"/api/v1/workflows/{missing}/report", headers=op)
        ).status_code == 404
        pending = await start(http, op, "Calculate 2 + 2 and think about it for a while")
        # Race-free check: whatever state it is in, a non-completed run answers 409, a completed
        # one 200 - never a server error.
        r = await http.post(
            f"/api/v1/workflows/{pending['id']}/ask", json={"question": "What was it?"}, headers=op
        )
        assert r.status_code in {200, 409}
        bad = await http.post(
            f"/api/v1/workflows/{pending['id']}/ask", json={"question": "x"}, headers=op
        )
        assert bad.status_code == 422


async def test_similar_past_research_is_suggested_not_reused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, **OFFLINE) as http:
        op = await as_user(http, "operator")
        run_id = await finished_run(http, op)
        close = await http.get(
            "/api/v1/workflows/similar",
            params={"goal": "Deep research on refund policy for digital goods"},
            headers=op,
        )
        assert [s["id"] for s in close.json()] == [run_id]
        assert close.json()[0]["similarity"] >= 0.5 and close.json()[0]["mode"] == "deep"
        far = await http.get(
            "/api/v1/workflows/similar", params={"goal": "Plan a holiday in Goa"}, headers=op
        )
        assert far.json() == []


async def test_comparison_goal_gets_a_question_per_side(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, **OFFLINE) as http:
        op = await as_user(http, "operator")
        created = await start(http, op, "Compare refund policy vs travel policy", mode="deep")
        run = await wait_status(http, op, created["id"], "COMPLETED", timeout=40)
        questions = [
            s["inputs"]["question"] for s in run["plan"]["steps"] if s["agent"] == "research"
        ]
        assert "refund policy" in questions[0] and "travel policy" in questions[1]
        assert any("differ" in q for q in questions)
