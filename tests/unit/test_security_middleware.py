"""Security middleware exercised on a tiny standalone app (no DB needed)."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core.errors import install_error_handlers
from app.core.ratelimit import InMemoryRateLimiter
from app.core.security_middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    client_ip,
)


def build_app(*, hsts: bool = False, max_bytes: int = 100, auth_limit: int = 2) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/api/v1/auth/login")
    async def login() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)
    app.add_middleware(
        RateLimitMiddleware,
        limiter=InMemoryRateLimiter(),
        default_per_minute=5,
        auth_per_minute=auth_limit,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    return app


@pytest.fixture
async def http() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=build_app(hsts=True), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def test_security_headers_present(http: AsyncClient) -> None:
    headers = (await http.get("/ping")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "max-age" in headers["strict-transport-security"]


async def test_csp_skipped_for_docs_and_hsts_off_by_default() -> None:
    app = build_app(hsts=False)

    @app.get("/docs")
    async def docs() -> str:
        return "x"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = (await client.get("/docs")).headers
    assert "content-security-policy" not in headers
    assert "strict-transport-security" not in headers


async def test_body_over_limit_rejected_by_content_length(http: AsyncClient) -> None:
    response = await http.post("/echo", content=b"x" * 101)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert (await http.post("/echo", content=b"x" * 100)).json() == {"size": 100}


async def test_streamed_body_over_limit_rejected_without_content_length(http: AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"x" * 40

    response = await http.post("/echo", content=chunks())
    assert response.status_code == 413


async def test_general_rate_limit_and_exemptions(http: AsyncClient) -> None:
    codes = [(await http.get("/ping")).status_code for _ in range(7)]
    assert codes == [200] * 5 + [429] * 2
    blocked = await http.get("/ping")
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["retry-after"]) >= 1
    for _ in range(20):  # health probes are never throttled
        assert (await http.get("/healthz")).status_code == 200


async def test_auth_endpoint_has_stricter_bucket(http: AsyncClient) -> None:
    codes = [(await http.post("/api/v1/auth/login")).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]
    assert (await http.get("/ping")).status_code == 200  # separate bucket


async def test_unhandled_error_is_generic(http: AsyncClient) -> None:
    response = await http.get("/boom")
    assert response.status_code == 500
    assert "secret internal detail" not in response.text
    assert response.json()["error"]["code"] == "internal_error"


async def test_options_not_rate_limited() -> None:
    app = build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for _ in range(10):
            assert (await client.options("/ping")).status_code != 429


def test_client_ip_proxy_trust() -> None:
    scope = {
        "type": "http",
        "client": ("10.0.0.1", 1234),
        "headers": [(b"x-forwarded-for", b"203.0.113.9, 10.0.0.1")],
    }
    assert client_ip(scope, trust_proxy_headers=False) == "10.0.0.1"
    assert client_ip(scope, trust_proxy_headers=True) == "203.0.113.9"
    assert client_ip({"type": "http", "headers": []}, trust_proxy_headers=False) == "unknown"
