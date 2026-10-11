"""A small, typed HTTP client for the platform's REST API.

What: every call the UI makes goes through ``ApiClient``. It adds the bearer token, silently
refreshes an expired access token once, and turns the API's error envelope
``{"error": {"code", "message", "details", "request_id"}}`` into one exception type (``ApiError``)
that the screens can show to the user.
Why the UI has no database access: the REST API is the only trusted boundary (authentication,
RBAC, audit, rate limits all live there). A UI that bypassed it would bypass all of them.
Why synchronous ``httpx``: Streamlit runs each script top to bottom per interaction, so plain
blocking calls are the natural fit; the client also accepts an injected ``httpx.Client`` so tests
can use ``MockTransport`` without a network.
Where tokens live: in this object, which Streamlit keeps in the per-browser-session server memory
(``st.session_state``). They are never written to a cookie, URL or file.
Connects to: ``streamlit_app.py`` and every module in ``views/``.
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx


class ApiError(Exception):
    """The API (or the network) said no. ``message`` is safe to show to the user."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.request_id = request_id

    def __str__(self) -> str:
        suffix = f" (request {self.request_id})" if self.request_id else ""
        return f"{self.message}{suffix}"


class ApiClient:
    def __init__(
        self, base_url: str, *, http: httpx.Client | None = None, timeout: float = 30.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(base_url=self.base_url, timeout=timeout)
        self._access: str | None = None
        self._refresh: str | None = None
        self.user: dict[str, Any] | None = None

    # ------------------------------------------------------------------ session
    @property
    def is_authenticated(self) -> bool:
        return self._access is not None and self.user is not None

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(self.user["roles"]) if self.user else frozenset()

    @property
    def email(self) -> str:
        return str(self.user["email"]) if self.user else ""

    def login(self, email: str, password: str) -> dict[str, Any]:
        tokens = self._request(
            "POST", "/auth/login", json={"email": email, "password": password}, auth=False
        )
        self._access, self._refresh = tokens["access_token"], tokens["refresh_token"]
        try:
            self.user = self._request("GET", "/auth/me")
        except ApiError:
            self._clear()
            raise
        assert self.user is not None
        return self.user

    def logout(self) -> None:
        """Revoke the refresh token on the server (best effort) and forget everything."""
        if self._refresh:
            # the local session ends either way, even if the server could not be told
            with contextlib.suppress(ApiError):
                self._request(
                    "POST", "/auth/logout", json={"refresh_token": self._refresh}, auth=False
                )
        self._clear()

    def _clear(self) -> None:
        self._access = self._refresh = None
        self.user = None

    # ------------------------------------------------------------------ transport
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
        raw: bool = False,
        _retried: bool = False,
    ) -> Any:
        merged = dict(headers or {})
        if auth and self._access:
            merged["Authorization"] = f"Bearer {self._access}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = self._http.request(
                method, f"/api/v1{path}", params=clean, json=json, headers=merged
            )
        except httpx.HTTPError as exc:
            raise ApiError(
                0, "unreachable", f"Cannot reach the API at {self.base_url} ({type(exc).__name__})"
            ) from exc
        if response.status_code == 401 and auth and not _retried and self._refresh:
            if self._try_refresh():
                return self._request(
                    method, path, params=params, json=json, headers=headers, raw=raw, _retried=True
                )
            self._clear()
            raise ApiError(401, "session_expired", "Your session expired. Please sign in again.")
        if response.status_code >= 400:
            raise _to_error(response)
        if raw:
            return response.content
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _try_refresh(self) -> bool:
        try:
            tokens = self._request(
                "POST", "/auth/refresh", json={"refresh_token": self._refresh}, auth=False
            )
        except ApiError:
            return False
        self._access, self._refresh = tokens["access_token"], tokens["refresh_token"]
        return True

    # ------------------------------------------------------------------ conversations
    def list_conversations(
        self, status: str | None = "ACTIVE", limit: int = 50
    ) -> list[dict[str, Any]]:
        page = self._request("GET", "/conversations", params={"status": status, "limit": limit})
        return list(page["items"])

    def create_conversation(self, title: str) -> dict[str, Any]:
        return self._request("POST", "/conversations", json={"title": title})  # type: ignore[no-any-return]

    def archive_conversation(self, conversation_id: str) -> dict[str, Any]:
        return self._request("POST", f"/conversations/{conversation_id}/archive")  # type: ignore[no-any-return]

    def list_messages(self, conversation_id: str, last: int = 100) -> list[dict[str, Any]]:
        """The most recent ``last`` messages, oldest first."""
        path = f"/conversations/{conversation_id}/messages"
        page = self._request("GET", path, params={"limit": last})
        if page["total"] > last:  # the API pages oldest-first; jump to the newest window
            page = self._request(
                "GET", path, params={"limit": last, "offset": page["total"] - last}
            )
        return list(page["items"])

    # ------------------------------------------------------------------ workflows
    def create_workflow(
        self,
        goal: str,
        *,
        conversation_id: str | None = None,
        max_revisions: int | None = None,
        idempotency_key: str | None = None,
        mode: str = "standard",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"goal": goal}
        if mode != "standard":
            body["mode"] = mode
        if conversation_id:
            body["conversation_id"] = conversation_id
        if max_revisions is not None:
            body["max_revisions"] = max_revisions
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/workflows", json=body, headers=headers)  # type: ignore[no-any-return]

    def similar_runs(self, goal: str) -> list[dict[str, Any]]:
        return list(self._request("GET", "/workflows/similar", params={"goal": goal}))

    def ask_followup(self, run_id: str, question: str) -> dict[str, Any]:
        return self._request("POST", f"/workflows/{run_id}/ask", json={"question": question})  # type: ignore[no-any-return]

    def download_report(self, run_id: str, fmt: str = "md") -> bytes:
        return self._request(  # type: ignore[no-any-return]
            "GET", f"/workflows/{run_id}/report", params={"format": fmt}, raw=True
        )

    def list_workflows(self, status: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        page = self._request("GET", "/workflows", params={"status": status, "limit": limit})
        return list(page["items"])

    def get_workflow(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workflows/{run_id}")  # type: ignore[no-any-return]

    def timeline(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workflows/{run_id}/timeline")  # type: ignore[no-any-return]

    def cancel_workflow(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/workflows/{run_id}/cancel")  # type: ignore[no-any-return]

    def retry_step(self, run_id: str, step_id: str) -> dict[str, Any]:
        return self._request("POST", f"/workflows/{run_id}/steps/{step_id}/retry")  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ approvals
    def list_approvals(
        self, status: str | None = "PENDING", limit: int = 50
    ) -> list[dict[str, Any]]:
        page = self._request("GET", "/approvals", params={"status": status, "limit": limit})
        return list(page["items"])

    def approve(self, approval_id: str, comment: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/approvals/{approval_id}/approve", json={"comment": comment})  # type: ignore[no-any-return]

    def reject(self, approval_id: str, comment: str) -> dict[str, Any]:
        return self._request("POST", f"/approvals/{approval_id}/reject", json={"comment": comment})  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ documents
    def list_documents(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._request("GET", "/documents", params={"limit": limit})["items"])

    def upload_document(self, title: str, content: str) -> dict[str, Any]:
        return self._request("POST", "/documents", json={"title": title, "content": content})  # type: ignore[no-any-return]

    def search_documents(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        return list(self._request("GET", "/documents/search", params={"q": query, "top_k": top_k}))

    def delete_document(self, document_id: str) -> None:
        self._request("DELETE", f"/documents/{document_id}")

    # ------------------------------------------------------------------ admin
    def list_users(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._request("GET", "/users", params={"limit": limit})["items"])

    def set_roles(self, user_id: str, roles: list[str]) -> dict[str, Any]:
        return self._request("PUT", f"/users/{user_id}/roles", json={"roles": roles})  # type: ignore[no-any-return]

    def set_active(self, user_id: str, active: bool) -> dict[str, Any]:
        return self._request("PUT", f"/users/{user_id}/active", json={"is_active": active})  # type: ignore[no-any-return]

    def audit_logs(
        self,
        *,
        action: str | None = None,
        resource_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._request(  # type: ignore[no-any-return]
            "GET",
            "/audit-logs",
            params={
                "action": action,
                "resource_type": resource_type,
                "limit": limit,
                "offset": offset,
            },
        )

    # ------------------------------------------------------------------ platform
    def readiness(self) -> dict[str, Any]:
        """``/readyz`` answers 503 with a useful body when a dependency is down."""
        try:
            response = self._http.get("/readyz")
        except httpx.HTTPError as exc:
            raise ApiError(
                0, "unreachable", f"Cannot reach the API ({type(exc).__name__})"
            ) from exc
        body: dict[str, Any] = response.json()
        return body

    def metrics_text(self, token: str | None = None) -> str:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = self._http.get("/metrics", headers=headers)
        except httpx.HTTPError as exc:
            raise ApiError(
                0, "unreachable", f"Cannot reach the API ({type(exc).__name__})"
            ) from exc
        if response.status_code >= 400:
            raise _to_error(response)
        return response.text


def _to_error(response: httpx.Response) -> ApiError:
    """Build an ``ApiError`` from the standard envelope (or a generic one for odd bodies)."""
    try:
        error = response.json()["error"]
        return ApiError(
            response.status_code,
            str(error.get("code", "error")),
            str(error.get("message", "Request failed")),
            error.get("details"),
            error.get("request_id"),
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return ApiError(
            response.status_code, "error", f"Request failed (HTTP {response.status_code})"
        )
