"""web_search, wikipedia_search and arxiv_search against stubbed HTTP services."""

import json

import httpx
import pytest

from app.tools.base import ToolContext, ToolError
from app.tools.builtin.research import (
    ArxivSearchTool,
    SearchInput,
    WebSearchTool,
    WikipediaSearchTool,
)
from tests.conftest import make_settings

ARXIV_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title>  Error   Correction
      at Scale </title>
    <summary>We study codes.
      They work.</summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
  </entry>
  <entry><id>http://arxiv.org/abs/2401.00002v1</id><title>No Authors</title>
    <summary>Just an abstract.</summary></entry>
</feed>"""

WIKI_JSON = {
    "query": {
        "pages": {
            "20": {"title": "Second hit", "index": 2, "extract": "Later article."},
            "10": {
                "title": "Quantum computing",
                "index": 1,
                "extract": "A computer type.\n\nMore.",
            },
        }
    }
}


def context(**settings: object) -> ToolContext:
    return ToolContext(principal=None, session_factory=None, settings=make_settings(**settings))  # type: ignore[arg-type]


def args(query: str = "quantum", n: int = 3) -> SearchInput:
    return SearchInput(query=query, max_results=n)


async def test_web_search_sends_key_and_shapes_results() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [{"title": "T1", "url": "https://a.test/1", "content": "snippet  one"}]
            },
        )

    out = await WebSearchTool(transport=httpx.MockTransport(handler)).run(
        args(n=9), context(tavily_api_key="tv-key", tavily_base_url="https://tv.test/")
    )
    request = seen[0]
    assert str(request.url) == "https://tv.test/search"
    assert request.headers["authorization"] == "Bearer tv-key"
    assert json.loads(request.content)["max_results"] == 5  # capped by RESEARCH_MAX_RESULTS
    assert out["source"] == "web" and out["count"] == 1
    assert out["results"][0] == {
        "citation": "Web: T1", "url": "https://a.test/1", "content": "snippet one",
    }  # fmt: skip


async def test_web_search_without_a_key_is_a_clear_non_retryable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request without a key")

    with pytest.raises(ToolError, match="TAVILY_API_KEY") as caught:
        await WebSearchTool(transport=httpx.MockTransport(handler)).run(args(), context())
    assert caught.value.retryable is False


async def test_wikipedia_orders_by_rank_and_builds_article_urls() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=WIKI_JSON)

    out = await WikipediaSearchTool(transport=httpx.MockTransport(handler)).run(
        args("quantum computing"), context(wikipedia_base_url="https://wiki.test")
    )
    assert seen[0].url.params["gsrsearch"] == "quantum computing"
    assert "workflow-platform" in seen[0].headers["user-agent"]
    first, second = out["results"]
    assert first["citation"] == "Wikipedia: Quantum computing"
    assert first["url"] == "https://wiki.test/wiki/Quantum_computing"
    assert first["content"] == "A computer type. More." and second["citation"].endswith(
        "Second hit"
    )


async def test_wikipedia_with_no_hits_returns_an_empty_list() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"batchcomplete": ""}))
    out = await WikipediaSearchTool(transport=transport).run(args(), context())
    assert out["count"] == 0 and out["results"] == []


async def test_arxiv_parses_atom_with_and_without_authors() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=ARXIV_XML.encode()))
    out = await ArxivSearchTool(transport=transport).run(args("error correction"), context())
    first, second = out["results"]
    assert first["citation"] == "arXiv: Error Correction at Scale"
    assert first["content"] == "Ada Lovelace, Alan Turing. We study codes. They work."
    assert first["url"] == "http://arxiv.org/abs/2401.00001v1"
    assert second["content"] == "Just an abstract."


async def test_arxiv_rejects_hostile_xml_instead_of_expanding_it() -> None:
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><feed>&a;</feed>'
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=bomb))
    with pytest.raises(Exception, match=r"(?i)entit|forbid"):
        await ArxivSearchTool(transport=transport).run(args(), context())


async def test_arxiv_invalid_xml_is_retryable() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"<feed>"))
    with pytest.raises(ToolError, match="invalid XML") as caught:
        await ArxivSearchTool(transport=transport).run(args(), context())
    assert caught.value.retryable is True


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (503, True), (401, False), (403, False), (400, False)],
)
async def test_http_errors_are_classified(status: int, retryable: bool) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status))
    with pytest.raises(ToolError) as caught:
        await WikipediaSearchTool(transport=transport).run(args(), context())
    assert caught.value.retryable is retryable


async def test_network_failures_are_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    for handler, text in ((timeout, "timed out"), (refused, "failed")):
        with pytest.raises(ToolError, match=text) as caught:
            await WikipediaSearchTool(transport=httpx.MockTransport(handler)).run(args(), context())
        assert caught.value.retryable is True


async def test_bad_json_is_retryable() -> None:
    for content in (b"not json", b"[1, 2]"):
        transport = httpx.MockTransport(lambda r, c=content: httpx.Response(200, content=c))
        with pytest.raises(ToolError) as caught:
            await WikipediaSearchTool(transport=transport).run(args(), context())
        assert caught.value.retryable is True


def test_input_is_validated() -> None:
    with pytest.raises(ValueError):
        SearchInput(query="x")
    with pytest.raises(ValueError):
        SearchInput(query="ok query", max_results=50)
    with pytest.raises(ValueError):
        SearchInput(query="ok query", surprise=1)  # type: ignore[call-arg]
