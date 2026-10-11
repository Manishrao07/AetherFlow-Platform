"""http_fetch against a fake transport: pinned IPs, redirect re-validation, caps."""

import httpx
import pytest

from app.tools.base import ToolContext, ToolError
from app.tools.builtin.http_fetch import HttpFetchInput, HttpFetchTool
from tests.conftest import make_settings

PUBLIC = "93.184.216.34"


def resolver(mapping: dict[str, str]):
    async def resolve(host: str, port: int) -> list[str]:
        if host not in mapping:
            raise OSError("nxdomain")
        return [mapping[host]]

    return resolve


def context(**settings: object) -> ToolContext:
    return ToolContext(principal=None, session_factory=None, settings=make_settings(**settings))  # type: ignore[arg-type]


def tool(handler, mapping: dict[str, str] | None = None) -> HttpFetchTool:
    return HttpFetchTool(
        resolver=resolver(mapping or {"example.com": PUBLIC}),
        transport=httpx.MockTransport(handler),
    )


def text_response(
    body: str = "hello", status: int = 200, ctype: str = "text/html; charset=utf-8", **headers: str
):
    return httpx.Response(status, content=body.encode(), headers={"content-type": ctype, **headers})


async def test_success_connects_to_validated_ip_with_original_host_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return text_response("<h1>Hi</h1>")

    result = await tool(handler).run(HttpFetchInput(url="https://example.com/page?q=1"), context())
    assert (
        result["status"] == 200 and result["text"] == "<h1>Hi</h1>" and result["truncated"] is False
    )
    assert seen[0].url.host == PUBLIC  # pinned: no second DNS lookup can redirect us
    assert seen[0].headers["host"] == "example.com"
    assert seen[0].extensions["sni_hostname"] == "example.com"


async def test_host_not_allowlisted_is_blocked_without_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be made")

    with pytest.raises(ToolError, match="allow-list"):
        await tool(handler).run(HttpFetchInput(url="https://evil.io/"), context())


async def test_hostname_resolving_to_internal_ip_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be made")

    with pytest.raises(ToolError, match="non-public"):
        await tool(handler, {"example.com": "169.254.169.254"}).run(
            HttpFetchInput(url="https://example.com/latest/meta-data"), context()
        )


async def test_redirect_to_internal_host_is_blocked_on_second_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://localhost/admin"})

    with pytest.raises(ToolError, match="blocked"):
        await tool(handler).run(HttpFetchInput(url="https://example.com/"), context())


async def test_redirect_to_allowed_host_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "/final"})
        return text_response("done")

    result = await tool(handler).run(HttpFetchInput(url="https://example.com/start"), context())
    assert result["text"] == "done" and result["url"].endswith("/final")


async def test_redirect_loop_gives_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/again"})

    with pytest.raises(ToolError, match="too many redirects"):
        await tool(handler).run(HttpFetchInput(url="https://example.com/"), context())


async def test_response_is_truncated_at_size_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return text_response("x" * 5000)

    result = await tool(handler).run(
        HttpFetchInput(url="https://example.com/"), context(http_fetch_max_bytes=1024)
    )
    assert result["truncated"] is True and len(result["text"]) == 1024


async def test_binary_content_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"\x00\x01", headers={"content-type": "application/octet-stream"}
        )

    with pytest.raises(ToolError, match="unsupported content type"):
        await tool(handler).run(HttpFetchInput(url="https://example.com/"), context())


async def test_server_error_and_timeout_are_retryable_client_error_is_not() -> None:
    with pytest.raises(ToolError) as server:
        await tool(lambda r: text_response("x", 503)).run(
            HttpFetchInput(url="https://example.com/"), context()
        )
    assert server.value.retryable

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ToolError) as timeout:
        await tool(slow).run(HttpFetchInput(url="https://example.com/"), context())
    assert timeout.value.retryable

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ToolError) as conn:
        await tool(broken).run(HttpFetchInput(url="https://example.com/"), context())
    assert conn.value.retryable

    result = await tool(lambda r: text_response("nope", 404)).run(
        HttpFetchInput(url="https://example.com/x"), context()
    )
    assert result["status"] == 404  # a 404 is a valid answer, not a failure


async def test_ipv6_target_is_bracketed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return text_response("v6")

    t = HttpFetchTool(
        resolver=resolver({"example.com": "2606:2800:220:1:248:1893:25c8:1946"}),
        transport=httpx.MockTransport(handler),
    )
    assert (await t.run(HttpFetchInput(url="https://example.com/"), context()))["text"] == "v6"
    assert seen[0].url.host == "2606:2800:220:1:248:1893:25c8:1946"
