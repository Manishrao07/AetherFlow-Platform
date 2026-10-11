"""Research tools: web_search (Tavily), wikipedia_search and arxiv_search.

What: three read-only tools that return cited snippets in the same ``results`` shape as the
knowledge-base search (``citation`` + ``content``), so every downstream piece (templating,
untrusted-text fencing, review) treats them like any other evidence.
Why fixed hosts: the model controls only the QUERY TEXT. Hosts come from settings, so there is
no SSRF surface (unlike ``http_fetch``, which takes a model-supplied URL and needs a guard).
Why LOW risk and no approval: they only read public data; a runaway plan is bounded by the
step limit, per-tool timeout and circuit breaker.
Why errors are classified: timeouts, 429 and 5xx are retryable (orchestrator backs off); a bad
key or bad request is not (retrying cannot help).
Connects to: ``builtin/__init__`` (registration), the research agent (Phase B), settings.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from defusedxml import ElementTree
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.models.enums import RiskLevel
from app.services.permissions import RESEARCH_SEARCH
from app.tools.base import Tool, ToolContext, ToolError

_USER_AGENT = "workflow-platform/1.0 (research tool; contact: ops@example.com)"
_ATOM = {"a": "http://www.w3.org/2005/Atom"}


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=300, examples=["quantum error correction"])
    max_results: int = Field(default=3, ge=1, le=10)


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace and cap length (snippets are evidence, not full documents)."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


class _HttpResearchTool(Tool):
    """Shared plumbing: one GET/POST with timeout and error classification."""

    risk = RiskLevel.LOW
    permission = RESEARCH_SEARCH
    timeout_seconds = 20.0
    uses_breaker = True
    input_model = SearchInput

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport  # injectable for tests; None = real network

    async def _request(
        self, settings: Settings, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=settings.research_timeout_seconds,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                response = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ToolError("search request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"search request failed: {type(exc).__name__}", retryable=True) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise ToolError(f"search service busy ({response.status_code})", retryable=True)
        if response.status_code in (401, 403):
            raise ToolError("search service rejected the credentials")
        if response.status_code >= 400:
            raise ToolError(f"search service rejected the request ({response.status_code})")
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise ToolError("search service returned invalid JSON", retryable=True) from exc
        if not isinstance(body, dict):
            raise ToolError("search service returned an unexpected payload", retryable=True)
        return body

    @staticmethod
    def _result(source: str, query: str, results: list[dict[str, str]]) -> dict[str, Any]:
        return {"source": source, "query": query, "count": len(results), "results": results}


class WebSearchTool(_HttpResearchTool):
    name = "web_search"
    description = "Search the live web (Tavily) and return titled, cited snippets."

    async def run(self, args: SearchInput, ctx: ToolContext) -> dict[str, Any]:
        settings = ctx.settings
        if settings.tavily_api_key is None or not settings.tavily_api_key.get_secret_value():
            raise ToolError("web search is not configured (set TAVILY_API_KEY)")
        response = await self._request(
            settings,
            "POST",
            f"{settings.tavily_base_url.rstrip('/')}/search",
            json={
                "query": args.query,
                "max_results": min(args.max_results, settings.research_max_results),
                "search_depth": "basic",
            },
            headers={"Authorization": f"Bearer {settings.tavily_api_key.get_secret_value()}"},
        )
        rows = self._json(response).get("results") or []
        results = [
            {
                "citation": f"Web: {_clip(str(r.get('title') or r.get('url') or 'result'), 80)}",
                "url": str(r.get("url") or ""),
                "content": _clip(str(r.get("content") or ""), settings.research_snippet_chars),
            }
            for r in rows
            if isinstance(r, dict)
        ]
        return self._result("web", args.query, results)


class WikipediaSearchTool(_HttpResearchTool):
    name = "wikipedia_search"
    description = "Search Wikipedia and return the introduction of the best-matching articles."

    async def run(self, args: SearchInput, ctx: ToolContext) -> dict[str, Any]:
        settings = ctx.settings
        base = settings.wikipedia_base_url.rstrip("/")
        limit = min(args.max_results, settings.research_max_results)
        response = await self._request(
            settings,
            "GET",
            f"{base}/w/api.php",
            params={
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": args.query, "gsrlimit": limit,
                "prop": "extracts", "exintro": 1, "explaintext": 1, "exlimit": "max",
            },
        )  # fmt: skip
        pages = (self._json(response).get("query") or {}).get("pages") or {}
        ordered = sorted(
            (p for p in pages.values() if isinstance(p, dict)), key=lambda p: p.get("index", 99)
        )
        results = [
            {
                "citation": f"Wikipedia: {p.get('title', 'article')}",
                "url": f"{base}/wiki/{quote(str(p.get('title', '')).replace(' ', '_'))}",
                "content": _clip(str(p.get("extract") or ""), settings.research_snippet_chars),
            }
            for p in ordered
        ]
        return self._result("wikipedia", args.query, results)


class ArxivSearchTool(_HttpResearchTool):
    name = "arxiv_search"
    description = "Search arXiv for academic papers and return titles, authors and abstracts."

    async def run(self, args: SearchInput, ctx: ToolContext) -> dict[str, Any]:
        settings = ctx.settings
        response = await self._request(
            settings,
            "GET",
            f"{settings.arxiv_base_url.rstrip('/')}/api/query",
            params={
                "search_query": f"all:{args.query}",
                "max_results": min(args.max_results, settings.research_max_results),
                "sortBy": "relevance",
            },
        )
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise ToolError("arXiv returned invalid XML", retryable=True) from exc
        results = []
        for entry in root.findall("a:entry", _ATOM):
            title = _clip(entry.findtext("a:title", "", _ATOM), 150)
            authors = [
                _clip(a.findtext("a:name", "", _ATOM), 60) for a in entry.findall("a:author", _ATOM)
            ][:4]
            summary = _clip(entry.findtext("a:summary", "", _ATOM), settings.research_snippet_chars)
            results.append(
                {
                    "citation": f"arXiv: {title}",
                    "url": (entry.findtext("a:id", "", _ATOM) or "").strip(),
                    "content": f"{', '.join(authors)}. {summary}" if authors else summary,
                }
            )
        return self._result("arxiv", args.query, results)
