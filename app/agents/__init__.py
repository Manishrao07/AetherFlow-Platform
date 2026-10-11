"""The six agents and the factory that wires them together."""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.base import AgentDeps
from app.agents.execution import ExecutionAgent
from app.agents.mock_handlers import register_mock_handlers
from app.agents.planner import PlannerAgent
from app.agents.research import ResearchAgent
from app.agents.retrieval import RetrievalAgent
from app.agents.review import ReviewAgent
from app.agents.synthesizer import SynthesizerAgent


@dataclass(frozen=True)
class AgentSet:
    planner: PlannerAgent
    retrieval: RetrievalAgent
    execution: ExecutionAgent
    review: ReviewAgent
    research: ResearchAgent
    synthesizer: SynthesizerAgent


def build_agents(deps: AgentDeps) -> AgentSet:
    """Create all agents; with the mock LLM, also register its deterministic handlers."""
    register_mock_handlers(deps.llm.provider)
    return AgentSet(
        planner=PlannerAgent(deps),
        retrieval=RetrievalAgent(deps),
        execution=ExecutionAgent(deps),
        review=ReviewAgent(deps),
        research=ResearchAgent(deps),
        synthesizer=SynthesizerAgent(deps),
    )
