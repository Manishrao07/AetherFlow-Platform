"""An in-memory stand-in for ``ApiClient`` so page tests run without a server.

What: ``StubApi`` subclasses the real client (so ``isinstance`` and the properties behave the same)
but answers every call from plain dicts, records the calls, and can be told to fail a method with
an ``ApiError``. Why: ``streamlit.testing.AppTest`` runs the real screens; only the network is
faked.
"""

from __future__ import annotations

from typing import Any

from ui.api_client import ApiClient, ApiError

NOW = "2026-01-01T10:00:00Z"
LATER = "2026-01-01T10:00:02Z"


def user(email: str, *roles: str) -> dict[str, Any]:
    return {"id": f"u-{email}", "email": email, "roles": list(roles), "full_name": email.title()}


def step(key: str, status: str = "SUCCEEDED", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": f"step-{key}", "step_key": key, "agent": "execution", "tool": "calculator",
        "status": status, "risk_level": "LOW", "requires_approval": False, "attempts": 1,
        "max_attempts": 3, "started_at": NOW, "finished_at": LATER, "position": 1, "revision": 0,
        "error": None, "inputs": {"expression": "1+1"}, "output": {"value": 2},
    }  # fmt: skip
    return base | extra


def run(status: str = "COMPLETED", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "run-1111111111", "goal": "Calculate 1+1", "status": status,
        "review_verdict": None, "review_score": None, "revision_count": 0, "max_revisions": 2,
        "started_at": NOW, "finished_at": LATER, "error": None,
        "result": {"summary": "The answer is 2."}, "plan": {"summary": "one step"},
    }  # fmt: skip
    return base | extra


def timeline(
    status: str = "COMPLETED", steps: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "run": run(status),
        "steps": steps if steps is not None else [step("calc")],
        "tool_executions": [
            {"step_id": "step-calc", "tool_name": "calculator", "attempt": 1,
             "status": "SUCCEEDED", "duration_ms": 3, "error": None}
        ],
        "llm_calls": [
            {"agent": "planner", "model": "mock", "status": "SUCCEEDED", "prompt_tokens": 10,
             "completion_tokens": 5, "latency_ms": 4, "cost_usd": "0.001", "cached": False}
        ],
        "approvals": [],
        "events": [{"type": "run.created", "data": {}, "created_at": NOW}],
        "totals": {"llm_calls": 1, "prompt_tokens": 10, "completion_tokens": 5,
                   "cost_usd": "0.001", "tool_executions": 1},
    }  # fmt: skip


def approval(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "appr-1", "run_id": "run-1111111111", "status": "PENDING", "risk_level": "HIGH",
        "goal": "Email ops", "requester_email": "operator@demo.local", "reason": "sends an email",
        "expires_at": LATER, "comment": None,
        "plan": {"steps": [{"key": "mail", "description": "send", "tool": "send_email",
                            "risk_level": "HIGH", "requires_approval": True,
                            "inputs": {"to": "ops@example.com"}}]},
    }  # fmt: skip
    return base | extra


class StubApi(ApiClient):
    """Fake client. ``fail["method"] = ApiError(...)`` makes that method raise."""

    def __init__(self, signed_in_as: dict[str, Any] | None = None) -> None:
        super().__init__("http://stub")
        self.accounts = {
            "admin@demo.local": ("pw", user("admin@demo.local", "admin")),
            "operator@demo.local": ("pw", user("operator@demo.local", "operator")),
            "viewer@demo.local": ("pw", user("viewer@demo.local", "viewer")),
        }
        if signed_in_as is not None:
            self._access, self.user = "token", signed_in_as
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail: dict[str, ApiError] = {}
        self.conversations: list[dict[str, Any]] = [{"id": "conv-1", "title": "Ops chat"}]
        self.messages: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = [run()]
        self.timelines: dict[str, dict[str, Any]] = {"run-1111111111": timeline()}
        self.approvals: list[dict[str, Any]] = [approval()]
        self.documents: list[dict[str, Any]] = []
        self.users: list[dict[str, Any]] = [
            {"id": "u1", "email": "viewer@demo.local", "full_name": "Vi", "roles": ["viewer"],
             "is_active": True}
        ]  # fmt: skip
        self.audit: dict[str, Any] = {"total": 0, "items": []}
        self.metrics = ""
        self.ready: dict[str, Any] = {"status": "ready", "checks": {"database": True}}
        self.workflow_status = "COMPLETED"
        self.similar: list[dict[str, Any]] = []

    def _do(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if name in self.fail:
            raise self.fail[name]

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [a for n, a, _ in self.calls if n == name]

    # --- session
    def login(self, email: str, password: str) -> dict[str, Any]:
        self._do("login", email)
        account = self.accounts.get(email)
        if account is None or account[0] != password:
            raise ApiError(401, "invalid_credentials", "Invalid email or password")
        self._access, self.user = "token", account[1]
        return account[1]

    def logout(self) -> None:
        self._do("logout")
        self._access, self.user = None, None

    # --- conversations
    def list_conversations(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self._do("list_conversations")
        return list(self.conversations)

    def create_conversation(self, title: str) -> dict[str, Any]:
        self._do("create_conversation", title)
        created = {"id": f"conv-{len(self.conversations) + 1}", "title": title}
        self.conversations.append(created)
        return created

    def archive_conversation(self, conversation_id: str) -> dict[str, Any]:
        self._do("archive_conversation", conversation_id)
        self.conversations = [c for c in self.conversations if c["id"] != conversation_id]
        return {}

    def list_messages(self, conversation_id: str, last: int = 100) -> list[dict[str, Any]]:
        self._do("list_messages", conversation_id)
        return list(self.messages)

    # --- workflows
    def create_workflow(self, goal: str, **kwargs: Any) -> dict[str, Any]:
        self._do("create_workflow", goal, **kwargs)
        if kwargs.get(
            "conversation_id"
        ):  # the platform records the message and, when done, the reply
            self.messages.append({"id": f"m{len(self.messages)}", "role": "user",
                                  "content": goal, "run_id": "run-new-0000000"})  # fmt: skip
            if self.workflow_status == "COMPLETED":
                self.messages.append({"id": f"m{len(self.messages)}", "role": "assistant",
                                      "content": "The answer is 2.", "run_id": None})  # fmt: skip
        return run("CREATED", goal=goal, id="run-new-0000000")

    def similar_runs(self, goal: str) -> list[dict[str, Any]]:
        self._do("similar_runs", goal)
        return list(self.similar)

    def ask_followup(self, run_id: str, question: str) -> dict[str, Any]:
        self._do("ask_followup", run_id, question)
        citation = {"n": 1, "tool": "search_knowledge_base", "citation": "Refund #1"}
        return {"answer": "Refundable until activation. [1]", "citations": [citation]}

    def download_report(self, run_id: str, fmt: str = "md") -> bytes:
        self._do("download_report", run_id, fmt)
        return b"%PDF-1.4 stub" if fmt == "pdf" else b"# Report"

    def list_workflows(self, status: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        self._do("list_workflows")
        return list(self.runs)

    def get_workflow(self, run_id: str) -> dict[str, Any]:
        self._do("get_workflow", run_id)
        return run(self.workflow_status, id=run_id)

    def timeline(self, run_id: str) -> dict[str, Any]:
        self._do("timeline", run_id)
        return self.timelines[run_id] if run_id in self.timelines else timeline()

    def cancel_workflow(self, run_id: str) -> dict[str, Any]:
        self._do("cancel_workflow", run_id)
        return run("CANCELLED")

    def retry_step(self, run_id: str, step_id: str) -> dict[str, Any]:
        self._do("retry_step", run_id, step_id)
        return run("EXECUTING")

    # --- approvals
    def list_approvals(
        self, status: str | None = "PENDING", limit: int = 50
    ) -> list[dict[str, Any]]:
        self._do("list_approvals", status)
        return [a for a in self.approvals if a["status"] == status]

    def approve(self, approval_id: str, comment: str | None = None) -> dict[str, Any]:
        self._do("approve", approval_id, comment)
        return {}

    def reject(self, approval_id: str, comment: str) -> dict[str, Any]:
        self._do("reject", approval_id, comment)
        return {}

    # --- documents
    def list_documents(self, limit: int = 100) -> list[dict[str, Any]]:
        self._do("list_documents")
        return list(self.documents)

    def upload_document(self, title: str, content: str) -> dict[str, Any]:
        self._do("upload_document", title, content)
        doc = {"id": "d1", "title": title, "chunk_count": 2, "source": "upload", "created_at": NOW}
        return {"document": doc, "created": True}

    def search_documents(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        self._do("search_documents", query)
        return [{"citation": "[Policy §1]", "score": 0.9, "content": "Refunds within 14 days."}]

    def delete_document(self, document_id: str) -> None:
        self._do("delete_document", document_id)
        self.documents = [d for d in self.documents if d["id"] != document_id]

    # --- admin / platform
    def list_users(self, limit: int = 100) -> list[dict[str, Any]]:
        self._do("list_users")
        return list(self.users)

    def set_roles(self, user_id: str, roles: list[str]) -> dict[str, Any]:
        self._do("set_roles", user_id, roles)
        for u in self.users:
            if u["id"] == user_id:
                u["roles"] = roles
        return {}

    def set_active(self, user_id: str, active: bool) -> dict[str, Any]:
        self._do("set_active", user_id, active)
        for u in self.users:
            if u["id"] == user_id:
                u["is_active"] = active
        return {}

    def audit_logs(self, **kwargs: Any) -> dict[str, Any]:
        self._do("audit_logs", **kwargs)
        return self.audit

    def readiness(self) -> dict[str, Any]:
        self._do("readiness")
        return self.ready

    def metrics_text(self, token: str | None = None) -> str:
        self._do("metrics_text")
        return self.metrics
