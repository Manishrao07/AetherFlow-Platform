"""ApiClient against a fake server (httpx MockTransport): auth, refresh, errors, helpers."""

import json
from collections.abc import Callable

import httpx
import pytest

from ui.api_client import ApiClient, ApiError

USER = {
    "id": "u1",
    "email": "op@demo.local",
    "full_name": "Op",
    "roles": ["operator"],
    "is_active": True,
}


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> ApiClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api.test")
    return ApiClient("http://api.test", http=http)


class FakeServer:
    """Records requests; answers login/me/refresh and anything registered in ``routes``."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}
        self.valid_access = "access-1"
        self.refresh_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1")
        if path == "/auth/login":
            return httpx.Response(
                200, json={"access_token": "access-1", "refresh_token": "refresh-1"}
            )
        if path == "/auth/refresh":
            if not self.refresh_ok:
                return httpx.Response(
                    401, json={"error": {"code": "invalid_token", "message": "no"}}
                )
            self.valid_access = "access-2"
            return httpx.Response(
                200, json={"access_token": "access-2", "refresh_token": "refresh-2"}
            )
        if path == "/auth/logout":
            return httpx.Response(204)
        if (
            request.headers.get("authorization") != f"Bearer {self.valid_access}"
            and path != "/metrics"
        ):
            return httpx.Response(
                401, json={"error": {"code": "unauthenticated", "message": "expired"}}
            )
        if path == "/auth/me":
            return httpx.Response(200, json=USER)
        handler = self.routes.get((request.method, path))
        if handler is None:
            return httpx.Response(404, json={"error": {"code": "not_found", "message": f"{path}?"}})
        return handler(request)


def logged_in() -> tuple[ApiClient, FakeServer]:
    server = FakeServer()
    api = make_client(server)
    api.login("op@demo.local", "pw")
    return api, server


def test_login_stores_tokens_and_user() -> None:
    api, server = logged_in()
    assert api.is_authenticated and api.email == "op@demo.local"
    assert api.roles == frozenset({"operator"})
    assert [r.url.path for r in server.requests] == ["/api/v1/auth/login", "/api/v1/auth/me"]
    assert "authorization" not in server.requests[0].headers  # login itself is anonymous


def test_a_failed_login_raises_the_server_message() -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"code": "invalid_credentials", "message": "Bad email or password",
                                 "request_id": "req-1"}}
        )  # fmt: skip

    api = make_client(deny)
    with pytest.raises(ApiError) as caught:
        api.login("x@y.z", "nope")
    assert caught.value.status == 401 and caught.value.code == "invalid_credentials"
    assert str(caught.value) == "Bad email or password (request req-1)"
    assert not api.is_authenticated and api.roles == frozenset() and api.email == ""


def test_login_forgets_the_tokens_when_the_profile_cannot_be_loaded() -> None:
    server = FakeServer()
    server.valid_access = "something-else"  # /auth/me will answer 401, and refresh gives access-2
    server.refresh_ok = False
    api = make_client(server)
    with pytest.raises(ApiError):
        api.login("a", "b")
    assert not api.is_authenticated


def test_an_expired_access_token_is_refreshed_once_and_the_call_retried() -> None:
    api, server = logged_in()
    server.valid_access = "rotated-on-server"  # the client's token is now stale
    server.routes[("GET", "/workflows")] = lambda r: httpx.Response(
        200, json={"items": [{"id": "r1"}], "total": 1, "limit": 30, "offset": 0}
    )
    server.valid_access = "access-2"  # what /auth/refresh will hand out
    server.refresh_ok = True
    assert api.list_workflows() == [{"id": "r1"}]
    paths = [r.url.path.removeprefix("/api/v1") for r in server.requests[-3:]]
    assert paths == ["/workflows", "/auth/refresh", "/workflows"]
    assert server.requests[-1].headers["authorization"] == "Bearer access-2"


def test_when_refresh_fails_the_session_ends_with_a_clear_error() -> None:
    api, server = logged_in()
    server.valid_access = "different"
    server.refresh_ok = False
    with pytest.raises(ApiError) as caught:
        api.list_workflows()
    assert caught.value.code == "session_expired" and not api.is_authenticated


def test_network_failure_becomes_an_api_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    api = make_client(down)
    with pytest.raises(ApiError) as caught:
        api.login("a", "b")
    assert caught.value.code == "unreachable" and "api.test" in caught.value.message
    with pytest.raises(ApiError):
        api.readiness()
    with pytest.raises(ApiError):
        api.metrics_text()


def test_non_json_error_bodies_are_still_reported() -> None:
    api = make_client(lambda r: httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(ApiError) as caught:
        api.login("a", "b")
    assert caught.value.status == 502 and "502" in caught.value.message


def test_logout_revokes_the_refresh_token_and_clears_state() -> None:
    api, server = logged_in()
    api.logout()
    assert not api.is_authenticated
    last = server.requests[-1]
    assert last.url.path.endswith("/auth/logout") and json.loads(last.content) == {
        "refresh_token": "refresh-1"
    }
    api.logout()  # idempotent; nothing to revoke


def test_logout_ignores_server_errors() -> None:
    api, server = logged_in()
    server.routes[("POST", "/auth/logout")] = lambda r: httpx.Response(500)

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": "x", "message": "boom"}})

    api._http = httpx.Client(transport=httpx.MockTransport(broken), base_url="http://api.test")
    api.logout()
    assert not api.is_authenticated


def test_create_workflow_sends_goal_options_and_idempotency_key() -> None:
    api, server = logged_in()
    server.routes[("POST", "/workflows")] = lambda r: httpx.Response(202, json={"id": "r1"})
    api.create_workflow(
        "Do the thing", conversation_id="c1", max_revisions=1, idempotency_key="k-1"
    )
    request = server.requests[-1]
    assert json.loads(request.content) == {
        "goal": "Do the thing",
        "conversation_id": "c1",
        "max_revisions": 1,
    }
    assert request.headers["idempotency-key"] == "k-1"
    api.create_workflow("Plain goal")
    assert json.loads(server.requests[-1].content) == {"goal": "Plain goal"}
    assert "idempotency-key" not in server.requests[-1].headers


def test_none_query_parameters_are_dropped() -> None:
    api, server = logged_in()
    server.routes[("GET", "/workflows")] = lambda r: httpx.Response(
        200, json={"items": [], "total": 0, "limit": 30, "offset": 0}
    )
    api.list_workflows(status=None, limit=5)
    assert dict(server.requests[-1].url.params) == {"limit": "5"}
    api.list_workflows(status="FAILED")
    assert dict(server.requests[-1].url.params) == {"status": "FAILED", "limit": "30"}


def test_list_messages_returns_the_newest_window() -> None:
    api, server = logged_in()
    seen: list[dict[str, str]] = []

    def messages(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        offset = int(params.get("offset", 0))
        items = [{"id": str(n)} for n in range(offset, min(offset + int(params["limit"]), 250))]
        return httpx.Response(
            200, json={"items": items, "total": 250, "limit": 100, "offset": offset}
        )

    server.routes[("GET", "/conversations/c1/messages")] = messages
    result = api.list_messages("c1", last=100)
    assert len(seen) == 2 and seen[1]["offset"] == "150"
    assert result[0]["id"] == "150" and result[-1]["id"] == "249"


def test_simple_endpoints_hit_the_expected_paths() -> None:
    api, server = logged_in()
    ok = lambda r: httpx.Response(200, json={"items": [], "total": 0, "limit": 1, "offset": 0})  # noqa: E731
    calls = [
        ("GET", "/conversations", lambda: api.list_conversations()),
        ("POST", "/conversations", lambda: api.create_conversation("t")),
        ("POST", "/conversations/c/archive", lambda: api.archive_conversation("c")),
        ("GET", "/workflows/r", lambda: api.get_workflow("r")),
        ("GET", "/workflows/r/timeline", lambda: api.timeline("r")),
        ("POST", "/workflows/r/cancel", lambda: api.cancel_workflow("r")),
        ("POST", "/workflows/r/steps/s/retry", lambda: api.retry_step("r", "s")),
        ("GET", "/approvals", lambda: api.list_approvals()),
        ("POST", "/approvals/a/approve", lambda: api.approve("a", "ok")),
        ("POST", "/approvals/a/reject", lambda: api.reject("a", "no")),
        ("GET", "/documents", lambda: api.list_documents()),
        ("POST", "/documents", lambda: api.upload_document("t", "c")),
        ("GET", "/documents/search", lambda: api.search_documents("q")),
        ("DELETE", "/documents/d", lambda: api.delete_document("d")),
        ("GET", "/users", lambda: api.list_users()),
        ("PUT", "/users/u/roles", lambda: api.set_roles("u", ["viewer"])),
        ("PUT", "/users/u/active", lambda: api.set_active("u", False)),
        ("GET", "/audit-logs", lambda: api.audit_logs(action="x", resource_type="y")),
    ]
    for method, path, _ in calls:
        server.routes[(method, path)] = ok
    server.routes[("GET", "/documents/search")] = lambda r: httpx.Response(200, json=[])
    server.routes[("DELETE", "/documents/d")] = lambda r: httpx.Response(204)
    for method, path, call in calls:
        call()
        assert (server.requests[-1].method, server.requests[-1].url.path) == (
            method,
            f"/api/v1{path}",
        )


def test_metrics_and_readiness() -> None:
    api, server = logged_in()
    server.routes[("GET", "/metrics")] = lambda r: httpx.Response(200, text="# metrics")
    assert api.metrics_text() == "# metrics"
    api.metrics_text("tok")
    assert server.requests[-1].headers["authorization"] == "Bearer tok"
    server.routes[("GET", "/metrics")] = lambda r: httpx.Response(
        401, json={"error": {"code": "unauthenticated", "message": "Missing metrics token"}}
    )
    with pytest.raises(ApiError):
        api.metrics_text()
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                503, json={"status": "not_ready", "checks": {"database": False}}
            )
        ),
        base_url="http://api.test",
    )
    assert ApiClient("http://api.test", http=http).readiness()["status"] == "not_ready"


def test_deep_mode_is_sent_only_when_asked_for() -> None:
    api, server = logged_in()
    bodies: list[dict[str, object]] = []

    def create(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"id": "r1"})

    server.routes[("POST", "/workflows")] = create
    api.create_workflow("Calculate 2+2")
    api.create_workflow("Research X", mode="deep")
    assert "mode" not in bodies[0] and bodies[1]["mode"] == "deep"


def test_report_helpers_hit_the_right_endpoints() -> None:
    api, server = logged_in()
    server.routes[("GET", "/workflows/similar")] = lambda r: httpx.Response(
        200, json=[{"id": "old", "similarity": 0.9}]
    )
    server.routes[("GET", "/workflows/r1/report")] = lambda r: httpx.Response(
        200, content=b"%PDF-1.4 x" if r.url.params["format"] == "pdf" else b"# md"
    )
    server.routes[("POST", "/workflows/r1/ask")] = lambda r: httpx.Response(
        200, json={"answer": json.loads(r.content)["question"], "citations": []}
    )
    assert api.similar_runs("goal")[0]["id"] == "old"
    assert api.download_report("r1", "pdf").startswith(b"%PDF")
    assert api.download_report("r1") == b"# md"
    assert api.ask_followup("r1", "why?")["answer"] == "why?"


def test_raw_downloads_survive_a_token_refresh() -> None:
    api, server = logged_in()
    server.routes[("GET", "/workflows/r1/report")] = lambda r: httpx.Response(200, content=b"ok")
    server.valid_access = "access-2"  # the client holds access-1 -> 401 -> refresh -> retry
    assert api.download_report("r1") == b"ok"
