"""ResearchAgent and SynthesizerAgent with a fake tool executor (no database, no network)."""

import json
from dataclasses import dataclass
from typing import Any

import pytest

from app.agents.base import AgentContext, AgentDeps
from app.agents.mock_handlers import register_mock_handlers
from app.agents.research import (
    NO_FINDINGS,
    ResearchAgent,
    ResearchInput,
    ResearchOutput,
    Source,
    validate_step_inputs,
)
from app.agents.synthesizer import (
    NO_EVIDENCE_REPORT,
    SynthesizerAgent,
    SynthesizerInput,
)
from app.core.llm.base import LLMRequest
from app.core.llm.client import build_llm_client
from app.core.llm.mock_provider import MockProvider
from app.tools.base import ToolError
from tests.conftest import make_principal, make_settings

ALL_SOURCES = ["search_knowledge_base", "wikipedia_search", "arxiv_search", "web_search"]


@dataclass
class _Result:
    output: dict[str, Any]


class FakeExecutor:
    """Returns canned passages per tool, or raises the configured ``ToolError``."""

    def __init__(self, passages: dict[str, list[dict[str, Any]]], errors: dict[str, ToolError]):
        self.passages, self.errors, self.calls = passages, errors, []

    async def execute(self, tool: str, inputs: dict[str, Any], *_: Any, **kw: Any) -> _Result:
        self.calls.append((tool, inputs, kw["plan_tools"]))
        if tool in self.errors:
            raise self.errors[tool]
        return _Result({"results": self.passages.get(tool, [])})


def passage(citation: str, content: str, url: str | None = None) -> dict[str, Any]:
    return {"citation": citation, "url": url, "content": content}


def make(
    passages: dict[str, list[dict[str, Any]]] | None = None,
    errors: dict[str, ToolError] | None = None,
) -> tuple[ResearchAgent, SynthesizerAgent, FakeExecutor, MockProvider]:
    settings = make_settings()
    llm = build_llm_client(settings)
    provider = llm.provider
    assert isinstance(provider, MockProvider)
    register_mock_handlers(provider)
    executor = FakeExecutor(passages or {}, errors or {})
    deps = AgentDeps(
        llm=llm,
        registry=None,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        session_factory=None,  # type: ignore[arg-type]
        settings=settings,
    )
    return ResearchAgent(deps), SynthesizerAgent(deps), executor, provider


def research_input(sources: list[str] | None = None) -> ResearchInput:
    chosen = sources or ALL_SOURCES
    return ResearchInput(
        question="What is quantum computing?", sources=chosen, plan_tools=frozenset(chosen)
    )


CTX = AgentContext(principal=make_principal())

WIKI = passage(
    "Wikipedia: Quantum computing",
    "Quantum computing uses qubits. It can solve some problems faster.",
    "https://en.wikipedia.org/wiki/Quantum_computing",
)
ARXIV = passage("arXiv: Error Correction", "Quantum computing needs error correction.")


async def test_sources_are_queried_in_parallel_and_numbered() -> None:
    agent, _, executor, _ = make({"wikipedia_search": [WIKI], "arxiv_search": [ARXIV]})
    out = await agent.run(research_input(), CTX)
    assert sorted(c[0] for c in executor.calls) == sorted(ALL_SOURCES)
    assert [s.id for s in out.sources] == ["S1", "S2"]
    assert out.sources[0].tool == "wikipedia_search" and out.sources[0].url == WIKI["url"]
    assert out.grounded and "[S" in out.findings
    assert all(c[2] == frozenset(ALL_SOURCES) for c in executor.calls)  # plan gate is passed on


async def test_knowledge_base_uses_top_k_and_web_tools_use_max_results() -> None:
    agent, _, executor, _ = make()
    await agent.run(research_input(), CTX)
    inputs = {tool: args for tool, args, _ in executor.calls}
    assert "top_k" in inputs["search_knowledge_base"]
    assert "max_results" in inputs["wikipedia_search"]


async def test_one_failing_source_is_recorded_not_fatal() -> None:
    agent, _, _, _ = make(
        {"wikipedia_search": [WIKI]},
        {"web_search": ToolError("TAVILY_API_KEY is not set", retryable=False)},
    )
    out = await agent.run(research_input(), CTX)
    assert out.grounded
    assert out.sources_failed == ["web_search: permanent: TAVILY_API_KEY is not set"]


@pytest.mark.parametrize(("transient", "expected"), [(True, True), (False, False)])
async def test_all_sources_failing_raises_with_the_right_retry_flag(
    transient: bool, expected: bool
) -> None:
    errors = {t: ToolError("boom", retryable=transient) for t in ALL_SOURCES}
    agent, _, _, _ = make(errors=errors)
    with pytest.raises(ToolError) as caught:
        await agent.run(research_input(), CTX)
    assert caught.value.retryable is expected


async def test_sources_that_find_nothing_give_an_ungrounded_answer_without_calling_the_llm() -> (
    None
):
    agent, _, _, provider = make()
    out = await agent.run(research_input(), CTX)
    assert out.findings == NO_FINDINGS and not out.grounded and out.sources == []
    assert provider.calls == []


async def test_invented_citations_are_dropped() -> None:
    agent, _, _, provider = make({"wikipedia_search": [WIKI]})
    provider.register(
        "research_findings",
        lambda _r: json.dumps({"findings": "Fact [S1] and fiction [S9].", "citations": ["S9"]}),
    )
    out = await agent.run(research_input(), CTX)
    assert out.findings == "Fact [S1] and fiction ." and not out.grounded


async def test_only_listed_sources_are_queried() -> None:
    agent, _, executor, _ = make({"arxiv_search": [ARXIV]})
    await agent.run(research_input(["arxiv_search"]), CTX)
    assert [c[0] for c in executor.calls] == ["arxiv_search"]


def test_step_inputs_validation() -> None:
    assert validate_step_inputs({"question": "What is it?", "sources": ["arxiv_search"]})
    with pytest.raises(ValueError, match="not allowed"):
        validate_step_inputs({"question": "What is it?", "sources": ["http_fetch"]})
    with pytest.raises(ValueError, match="invalid research inputs"):
        validate_step_inputs({"question": "x", "sources": []})


def research_output(question: str, findings: str, sources: list[Source]) -> dict[str, Any]:
    return ResearchOutput(
        question=question,
        findings=findings,
        sources=sources,
        sources_failed=[],
        grounded=bool(sources),
    ).model_dump(mode="json")


def src(source_id: str, citation: str, url: str | None = None) -> Source:
    return Source(id=source_id, tool="wikipedia_search", citation=citation, url=url, snippet="s")


async def test_synthesizer_numbers_sources_globally_and_merges_duplicates() -> None:
    _, synth, _, provider = make()
    upstream = {
        "s1": research_output(
            "Q one?",
            "A [S1] and B [S2].",
            [src("S1", "Wiki A", "http://a"), src("S2", "Wiki B", "http://b")],
        ),
        "s2": research_output("Q two?", "C [S1].", [src("S1", "Wiki A again", "http://a")]),
    }
    out = await synth.run(SynthesizerInput(topic="Quantum things", upstream=upstream), CTX)
    request: LLMRequest = provider.calls[0]
    assert (
        "[1] Wiki A" in request.messages[0].content and "[2] Wiki B" in request.messages[0].content
    )
    assert [s.n for s in out.sources] == [1, 2]  # the shared URL is one reference
    assert "## Sources" in out.report and "1. Wiki A - http://a" in out.report
    assert out.questions_total == 2 and out.questions_grounded == 2 and out.coverage == 1.0


async def test_synthesizer_strips_invented_numbers_and_lists_only_cited_sources() -> None:
    _, synth, _, provider = make()
    provider.register(
        "synthesize_report",
        lambda _r: json.dumps({"report": "# T\n\nClaim [1] and ghost [7].", "citations": [1, 7]}),
    )
    upstream = {"s1": research_output("Q?", "A [S1] B [S2].", [src("S1", "One"), src("S2", "Two")])}
    out = await synth.run(SynthesizerInput(topic="Topic here", upstream=upstream), CTX)
    assert "[7]" not in out.report and "Claim [1] and ghost ." in out.report
    assert [s.n for s in out.sources] == [1]
    assert "2. Two" not in out.report


async def test_synthesizer_without_any_source_skips_the_llm() -> None:
    _, synth, _, provider = make()
    upstream = {"s1": research_output("Q?", NO_FINDINGS, [])}
    out = await synth.run(SynthesizerInput(topic="Obscure topic", upstream=upstream), CTX)
    assert out.report == NO_EVIDENCE_REPORT and out.coverage == 0.0 and out.sources == []
    assert provider.calls == []


async def test_synthesizer_coverage_counts_grounded_questions() -> None:
    _, synth, _, _ = make()
    upstream = {
        "s1": research_output("Q1?", "A [S1].", [src("S1", "One", "http://1")]),
        "s2": research_output("Q2?", NO_FINDINGS, []),
    }
    out = await synth.run(SynthesizerInput(topic="Mixed topic", upstream=upstream), CTX)
    assert out.questions_total == 2 and out.questions_grounded == 1 and out.coverage == 0.5


async def test_cited_sources_are_renumbered_without_gaps() -> None:
    _, synth, _, provider = make()
    provider.register(
        "synthesize_report",
        lambda _r: json.dumps({"report": "# T\n\nFirst [1] and third [3].", "citations": [1, 3]}),
    )
    upstream = {
        "s1": research_output(
            "Q?", "A [S1] B [S2] C [S3].", [src("S1", "One"), src("S2", "Two"), src("S3", "Three")]
        )
    }
    out = await synth.run(SynthesizerInput(topic="Topic here", upstream=upstream), CTX)
    assert "First [1] and third [2]." in out.report
    assert [(s.n, s.citation) for s in out.sources] == [(1, "One"), (2, "Three")]
    assert "2. Three" in out.report and "Two" not in out.report
