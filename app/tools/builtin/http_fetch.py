"""http_fetch: fetch a web page, safely.

Defences (OWASP SSRF): host allow-list -> resolve DNS -> every address must be public ->
connect to the VALIDATED IP (no second lookup, so no DNS rebinding) -> redirects are followed
manually and each hop is re-validated -> response size is capped while streaming -> timeout.
The response is returned as DATA; agents wrap it as untrusted text before showing it to a model.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.netguard import Resolver, SafeTarget, UnsafeUrlError, default_resolver, validate_url
from app.models.enums import RiskLevel
from app.services.permissions import HTTP_FETCH
from app.tools.base import Tool, ToolContext, ToolError

_MAX_REDIRECTS = 3
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")


class HttpFetchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=2000, examples=["https://example.com/"])


class HttpFetchTool(Tool):
    name = "http_fetch"
    description = "HTTP GET a page from an allow-listed host and return its text."
    input_model = HttpFetchInput
    risk = RiskLevel.MEDIUM
    permission = HTTP_FETCH
    timeout_seconds = 15.0
    uses_breaker = True

    def __init__(
        self,
        *,
        resolver: Resolver = default_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._resolver = resolver
        self._transport = transport  # injectable for tests; None = real network

    async def run(self, args: HttpFetchInput, ctx: ToolContext) -> dict[str, Any]:
        settings = ctx.settings
        url = args.url
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=settings.http_fetch_timeout_seconds,
            follow_redirects=False,  # WHY: we follow manually so every hop is validated
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                try:
                    target = await validate_url(
                        url, settings.http_fetch_allowed_hosts, self._resolver
                    )
                except UnsafeUrlError as exc:
                    raise ToolError(f"blocked: {exc}") from exc
                response, body = await self._get(client, target, settings.http_fetch_max_bytes)
                if response.is_redirect and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    continue
                return self._result(url, response, body, settings.http_fetch_max_bytes)
        raise ToolError("too many redirects")

    async def _get(
        self, client: httpx.AsyncClient, target: SafeTarget, max_bytes: int
    ) -> tuple[httpx.Response, bytes]:
        pinned = target.ip if ":" not in target.ip else f"[{target.ip}]"
        request = client.build_request(
            "GET",
            httpx.URL(target.url).copy_with(host=pinned),
            headers={"Host": target.host, "User-Agent": "workflow-platform/1.0"},
            extensions={"sni_hostname": target.host},  # TLS cert is checked against the real name
        )
        try:
            response = await client.send(request, stream=True)
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                chunks.append(chunk)
                if size > max_bytes:  # WHY: stop downloading, do not buffer a 10 GB response
                    break
            await response.aclose()
        except httpx.TimeoutException as exc:
            raise ToolError("request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"request failed: {type(exc).__name__}", retryable=True) from exc
        return response, b"".join(chunks)[: max_bytes + 1]

    @staticmethod
    def _result(url: str, response: httpx.Response, body: bytes, max_bytes: int) -> dict[str, Any]:
        if response.status_code >= 500:
            raise ToolError(f"upstream error {response.status_code}", retryable=True)
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not content_type.startswith(_TEXT_TYPES):
            raise ToolError(f"unsupported content type '{content_type or 'unknown'}'")
        truncated = len(body) > max_bytes
        return {
            "url": url,
            "status": response.status_code,
            "content_type": content_type,
            "truncated": truncated,
            "text": body[:max_bytes].decode("utf-8", errors="replace"),
        }
