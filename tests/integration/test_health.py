"""Health endpoints and request-ID middleware against the real app + DB."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine


async def test_healthz(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


async def test_readyz_ok_with_database(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": True, "llm": True}}


async def test_readyz_503_when_database_unreachable(client: AsyncClient) -> None:
    # Point the app at a closed port to simulate an outage.
    client.app.state.engine = create_async_engine(  # type: ignore[attr-defined]
        "postgresql+asyncpg://wf:wf@127.0.0.1:1/none", connect_args={"timeout": 1}
    )
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": False, "llm": True}


async def test_request_id_is_generated_and_echoed(client: AsyncClient) -> None:
    generated = (await client.get("/healthz")).headers["x-request-id"]
    assert len(generated) == 32
    echoed = await client.get("/healthz", headers={"X-Request-ID": "trace-abc-12345"})
    assert echoed.headers["x-request-id"] == "trace-abc-12345"


async def test_malicious_request_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "bad id\twith spaces!"})
    assert response.headers["x-request-id"] != "bad id\twith spaces!"
    assert len(response.headers["x-request-id"]) == 32
