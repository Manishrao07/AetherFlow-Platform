"""Goal -> plan -> execute -> review, end to end on the mock LLM."""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Message
from tests.e2e.helpers import (
    as_user,
    flow_app,
    parse_data,
    read_sse,
    start,
    status_path,
    step_by_key,
    timeline,
    wait_status,
)

GOAL = "What is the refund window for digital goods?"


async def test_simple_goal_completes_with_cited_answer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        created = await start(http, op, GOAL)
        assert created["status"] in {"CREATED", "PLANNING", "EXECUTING"}
        run = await wait_status(http, op, created["id"], "COMPLETED")
        assert run["review_verdict"] == "APPROVED" and float(run["review_score"]) >= 0.7
        assert run["plan"]["steps"][0]["tool"] == "search_knowledge_base"
        assert "Refund Policy" in run["result"]["summary"]

        tl = await timeline(http, op, created["id"])
        assert status_path(tl) == ["PLANNING", "EXECUTING", "REVIEWING", "COMPLETED"]
        step = step_by_key(tl, "s1")
        assert step["status"] == "SUCCEEDED" and step["attempts"] == 1
        assert step["output"]["grounded"] is True and step["output"]["citations"]
        assert [t["tool_name"] for t in tl["tool_executions"]] == ["search_knowledge_base"]
        assert sorted({c["agent"] for c in tl["llm_calls"]}) == ["planner", "retrieval", "review"]
        assert tl["totals"]["llm_calls"] == 3 and tl["totals"]["prompt_tokens"] > 0
        assert tl["approvals"] == []
        types = [e["type"] for e in tl["events"]]
        assert types[0] == "run.created" and "plan.created" in types and "review.verdict" in types


async def test_run_without_approval_needs_no_human(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, "Calculate 1200 * 0.18 for the refund fee")
        done = await wait_status(http, op, run["id"], "COMPLETED")
        tl = await timeline(http, op, done["id"])
        assert step_by_key(tl, "s2")["output"]["output"]["result"] == 216
        approvals = await http.get("/api/v1/approvals?status=PENDING", headers=op)
        assert approvals.json()["total"] == 0


async def test_conversation_gets_user_message_and_assistant_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        cid = (
            await http.post("/api/v1/conversations", json={"title": "Policies"}, headers=op)
        ).json()["id"]
        run = await start(http, op, GOAL, conversation_id=cid)
        await wait_status(http, op, run["id"], "COMPLETED")
        history = (await http.get(f"/api/v1/conversations/{cid}/messages", headers=op)).json()[
            "items"
        ]
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert history[0]["content"] == GOAL and history[1]["run_id"] == run["id"]
        assert "Refund Policy" in history[1]["content"]
    async with session_factory() as s:
        assert await s.scalar(sa.select(sa.func.count()).select_from(Message)) == 2


async def test_second_run_in_same_conversation_sees_the_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        cid = (await http.post("/api/v1/conversations", json={"title": "c"}, headers=op)).json()[
            "id"
        ]
        first = await start(http, op, GOAL, conversation_id=cid)
        await wait_status(http, op, first["id"], "COMPLETED")
        provider = http.app.state.llm.provider  # type: ignore[attr-defined]
        provider.calls.clear()
        second = await start(http, op, "And what about shipping fees?", conversation_id=cid)
        await wait_status(http, op, second["id"], "COMPLETED")
        planner_prompt = next(c for c in provider.calls if c.task == "plan").messages[0].content
        assert "<conversation_context>" in planner_prompt and "refund window" in planner_prompt


async def test_long_term_memory_reaches_the_planner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        await http.put(
            "/api/v1/memory/style",
            json={"content": "Prefers refund answers in bullet points"},
            headers=op,
        )
        run = await start(http, op, "Summarise the refund policy")
        await wait_status(http, op, run["id"], "COMPLETED")
        provider = http.app.state.llm.provider  # type: ignore[attr-defined]
        prompt = next(c for c in provider.calls if c.task == "plan").messages[0].content
        assert "bullet points" in prompt and 'source="long-term memory"' in prompt


async def test_sse_replays_history_and_resumes_after_last_event_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "COMPLETED")
        events = await read_sse(http, op, run["id"])
        assert events[-1]["event"] == "end" and parse_data(events[-1]) == {"status": "COMPLETED"}
        ids = [int(e["id"]) for e in events if "id" in e]
        assert ids == sorted(ids) and len(ids) >= 8
        names = [e["event"] for e in events]
        assert names[0] == "run.created" and "step.status" in names and "review.verdict" in names
        resumed = await read_sse(http, op, run["id"], **{"Last-Event-ID": str(ids[-3])})
        assert [int(e["id"]) for e in resumed if "id" in e] == ids[-2:]


async def test_sse_requires_auth_and_visibility(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, GOAL)
        url = f"/api/v1/workflows/{run['id']}/events"
        assert (await http.get(url)).status_code == 401
        assert (
            await http.get(url, headers={"Authorization": "Bearer nonsense"})
        ).status_code == 401
        other = await as_user(http, "viewer")
        assert (await http.get(url, headers=other)).status_code == 404  # not yours
        await wait_status(http, op, run["id"], "COMPLETED")
