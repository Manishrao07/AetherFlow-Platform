"""Hardening on the real app: error envelope, headers, size limit, rate limit, CORS."""

from httpx import AsyncClient

from tests.conftest import running_app


async def test_unknown_route_and_method_use_envelope(client: AsyncClient) -> None:
    missing = await client.get("/nope")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    wrong_method = await client.delete("/healthz")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["request_id"] == wrong_method.headers["x-request-id"]


async def test_validation_error_does_not_echo_input(client: AsyncClient) -> None:
    r = await client.post("/api/v1/auth/login", json={"email": "x@y.zz", "password": "p" * 200})
    assert r.status_code == 422
    assert "ppppp" not in r.text and r.json()["error"]["code"] == "validation_error"


async def test_security_headers_on_real_app(client: AsyncClient) -> None:
    headers = (await client.get("/healthz")).headers
    assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"


async def test_swagger_docs_still_load(client: AsyncClient) -> None:
    r = await client.get("/docs")
    assert r.status_code == 200 and "content-security-policy" not in r.headers
    assert (await client.get("/openapi.json")).status_code == 200


async def test_oversized_body_rejected_before_parsing(migrated_database: None) -> None:
    async with running_app(max_request_bytes=1024) as http:
        r = await http.post("/api/v1/auth/login", content=b"{" + b" " * 2000 + b"}")
        assert r.status_code == 413 and r.json()["error"]["request_id"]


async def test_login_is_rate_limited(migrated_database: None) -> None:
    async with running_app(rate_limit_enabled=True, rate_limit_auth_per_minute=3) as http:
        body = {"email": "nobody@example.com", "password": "whatever-1"}
        codes = [(await http.post("/api/v1/auth/login", json=body)).status_code for _ in range(5)]
        assert codes == [401, 401, 401, 429, 429]
        assert (await http.get("/healthz")).status_code == 200  # probes exempt


async def test_cors_only_allows_configured_origin(migrated_database: None) -> None:
    async with running_app(cors_origins="http://localhost:8501") as http:
        good = await http.get("/healthz", headers={"Origin": "http://localhost:8501"})
        evil = await http.get("/healthz", headers={"Origin": "http://evil.example"})
        assert good.headers["access-control-allow-origin"] == "http://localhost:8501"
        assert "access-control-allow-origin" not in evil.headers
