"""Deep-research planning: sub-questions in, a fixed research -> synthesis DAG out."""

import json

import pytest

from app.agents.base import AgentContext, AgentOutputError
from app.agents.planner import PlannerInput
from app.models.enums import AgentName, RiskLevel, RunMode
from tests.conftest import make_agents, make_principal


def ctx(*roles: str) -> AgentContext:
    return AgentContext(principal=make_principal(*roles))


def deep(goal: str = "Deep research on quantum computing") -> PlannerInput:
    return PlannerInput(goal=goal, mode=RunMode.DEEP)


async def test_deep_plan_is_parallel_research_steps_then_one_synthesis() -> None:
    agents, provider = make_agents()
    plan = await agents.planner.run(deep(), ctx())
    research = [s for s in plan.steps if s.agent is AgentName.RESEARCH]
    last = plan.steps[-1]
    assert len(research) == 4 and last.agent is AgentName.SYNTHESIZER
    assert last.depends_on == [s.key for s in research]
    assert plan.layers() == [[s.key for s in research], [last.key]]
    assert all(s.tool is None and s.risk_level is RiskLevel.LOW for s in plan.steps)
    assert "quantum computing" in research[0].inputs["question"]
    assert provider.calls[0].task == "plan_deep"


async def test_sources_exclude_web_search_without_a_key_and_include_it_with_one() -> None:
    agents, _ = make_agents()
    plan = await agents.planner.run(deep(), ctx())
    sources = plan.steps[0].inputs["sources"]
    assert "web_search" not in sources and "wikipedia_search" in sources
    assert "search_knowledge_base" in sources

    keyed, _ = make_agents(tavily_api_key="tvly-test")
    with_key = await keyed.planner.run(deep(), ctx())
    assert "web_search" in with_key.steps[0].inputs["sources"]


async def test_plan_tools_include_the_research_sources() -> None:
    agents, _ = make_agents()
    plan = await agents.planner.run(deep(), ctx())
    assert {"wikipedia_search", "arxiv_search", "search_knowledge_base"} <= plan.tools


async def test_question_count_is_capped_by_settings() -> None:
    agents, provider = make_agents(deep_research_max_questions=2)
    provider.register(
        "plan_deep",
        lambda _r: json.dumps(
            {"summary": "s", "sub_questions": [f"Question number {i}?" for i in range(6)]}
        ),
    )
    plan = await agents.planner.run(deep(), ctx())
    assert [s.agent for s in plan.steps].count(AgentName.RESEARCH) == 2


async def test_duplicate_questions_collapse() -> None:
    agents, provider = make_agents()
    provider.register(
        "plan_deep",
        lambda _r: json.dumps({"summary": "s", "sub_questions": ["Same thing?", "Same thing?"]}),
    )
    plan = await agents.planner.run(deep(), ctx())
    assert len(plan.steps) == 2  # one researcher + the synthesizer


async def test_unparseable_sub_questions_fail_after_one_repair() -> None:
    agents, provider = make_agents()
    provider.register("plan_deep", lambda _r: "not json at all")
    with pytest.raises(AgentOutputError):
        await agents.planner.run(deep(), ctx())
    assert len(provider.calls) == 2


async def test_standard_planner_rejects_research_steps() -> None:
    agents, provider = make_agents()
    provider.register(
        "plan",
        lambda _r: json.dumps(
            {
                "summary": "sneaky",
                "steps": [
                    {
                        "key": "s1", "description": "research", "agent": "research",
                        "inputs": {"question": "What is it?", "sources": ["web_search"]},
                    }
                ],
            }
        ),
    )  # fmt: skip
    with pytest.raises(AgentOutputError, match="deep-only"):
        await agents.planner.run(PlannerInput(goal="Do something odd"), ctx())


async def test_a_caller_with_no_permitted_source_cannot_get_a_deep_plan() -> None:
    agents, _ = make_agents()
    with pytest.raises(AgentOutputError, match="deep research plan is invalid"):
        await agents.planner.run(deep(), ctx("nobody"))
