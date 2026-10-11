"""Guided demo of the platform: ``python scripts/demo.py`` (or ``make demo``).

What: drives the REAL running API the way the web UI does and narrates what happens - a grounded
answer from the knowledge base, a tool call, the review loop, a plan that needs human approval
(including the separation-of-duties refusal), role restrictions, idempotent retries, and the
audit trail. It exits non-zero if anything behaves differently from what the docs promise, so it
doubles as a smoke test of a fresh deployment.
Why plain ``httpx`` and not the UI client: this script runs inside the API image, which does not
contain the ``ui`` package.
Needs: a running API with seeded demo users (``make seed``). Settings: ``API_BASE_URL`` (default
http://localhost:8000) and ``SEED_DEMO_PASSWORD`` (default ``Demo#Pass123``).
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any

import httpx

DEFAULT_PASSWORD = "Demo#Pass123"
FINAL = {"COMPLETED", "FAILED", "CANCELLED"}


class DemoError(Exception):
    """The platform did not behave as documented."""


class Demo:
    """A tiny scripted client: one logged-in session per demo role."""

    def __init__(self, http: httpx.Client, password: str, wait_seconds: float = 60.0) -> None:
        self.http = http
        self.password = password
        self.wait_seconds = wait_seconds
        self._tokens: dict[str, str] = {}

    # ------------------------------------------------------------------ plumbing
    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def step(self, title: str) -> None:
        self.say(f"\n== {title}")

    def expect(self, condition: bool, message: str) -> None:
        if not condition:
            raise DemoError(message)
        self.say(f"   ok: {message}")

    def login(self, role: str) -> None:
        response = self.http.post(
            "/api/v1/auth/login", json={"email": f"{role}@demo.local", "password": self.password}
        )
        if response.status_code != 200:
            raise DemoError(
                f"login as {role} failed ({response.status_code}); did you run `make seed`?"
            )
        self._tokens[role] = response.json()["access_token"]

    def call(
        self, role: str, method: str, path: str, *, expect: int | tuple[int, ...] = 200, **kw: Any
    ) -> Any:
        """Call the API as ``role`` and check the status code."""
        headers = {"Authorization": f"Bearer {self._tokens[role]}", **kw.pop("headers", {})}
        response = self.http.request(method, f"/api/v1{path}", headers=headers, **kw)
        allowed = (expect,) if isinstance(expect, int) else expect
        if response.status_code not in allowed:
            raise DemoError(f"{method} {path} as {role}: {response.status_code} {response.text}")
        return response.json() if response.content else None

    def wait_for(self, role: str, run_id: str, wanted: set[str]) -> dict[str, Any]:
        """Poll a run until it reaches one of ``wanted`` (or fail after ``wait_seconds``)."""
        deadline = time.monotonic() + self.wait_seconds
        while True:
            run: dict[str, Any] = self.call(role, "GET", f"/workflows/{run_id}")
            if run["status"] in wanted:
                return run
            if run["status"] in FINAL or time.monotonic() > deadline:
                raise DemoError(f"run {run_id[:8]} ended as {run['status']}: {run.get('error')}")
            time.sleep(0.3)

    def start(self, role: str, goal: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        run: dict[str, Any] = self.call(
            role, "POST", "/workflows", json={"goal": goal}, headers=headers or {},
            expect=(200, 202),
        )  # fmt: skip
        self.say(f"   started run {run['id'][:8]}: {goal}")
        return run

    def show_timeline(self, role: str, run_id: str) -> dict[str, Any]:
        tl: dict[str, Any] = self.call(role, "GET", f"/workflows/{run_id}/timeline")
        for s in tl["steps"]:
            tool = s["tool"] or s["agent"]
            self.say(f"   rev {s['revision']} step {s['step_key']:<6} {tool:<22} {s['status']}")
        t = tl["totals"]
        self.say(
            f"   {t['llm_calls']} LLM calls, {t['prompt_tokens']}/{t['completion_tokens']} "
            f"tokens in/out, cost ${t['cost_usd']}, {t['tool_executions']} tool calls"
        )
        return tl

    # ------------------------------------------------------------------ the story
    def run(self) -> None:
        self.step("0. Platform is up")
        ready = self.http.get("/readyz").json()
        self.expect(ready["status"] == "ready", f"readiness: {ready['checks']}")
        for role in ("admin", "approver", "operator", "viewer"):
            self.login(role)
        self.say("   signed in as admin, approver, operator and viewer")

        self.grounded_answer()
        self.review_loop()
        self.approval_flow()
        self.role_limits()
        self.idempotency()
        self.audit_trail()
        self.say("\nDemo finished: every check above passed.")

    def grounded_answer(self) -> None:
        self.step("1. A grounded answer: plan -> retrieve from the knowledge base -> review")
        run = self.start("operator", "What is the refund window for digital goods?")
        done = self.wait_for("operator", run["id"], {"COMPLETED"})
        self.say(f"   answer: {(done.get('result') or {}).get('summary', '')[:200]}")
        self.show_timeline("operator", run["id"])
        self.expect(done["review_verdict"] == "APPROVED", "the Review agent approved the answer")

    def review_loop(self) -> None:
        self.step("2. Review can send a plan back for revision (mock-LLM test hook)")
        run = self.start("operator", "Calculate 1200 * 0.18 for the refund fee [force-revision]")
        done = self.wait_for("operator", run["id"], {"COMPLETED"})
        self.say(
            f"   revisions used: {done['revision_count']}/{done['max_revisions']} "
            "(0 when a real model is configured, since the hook is mock-only)"
        )
        self.show_timeline("operator", run["id"])

    def approval_flow(self) -> None:
        self.step("3. A HIGH-risk step pauses the run until a human approves")
        # WHY an admin starts this run: admins HAVE the approval permission, so the refusal below
        # is the separation-of-duties rule itself, not just a missing role.
        goal = "Check the SEV2 incident policy, open a ticket and email ops@example.com"
        run = self.start("admin", goal)
        paused = self.wait_for("admin", run["id"], {"AWAITING_APPROVAL"})
        self.expect(paused["status"] == "AWAITING_APPROVAL", "nothing ran before approval")
        pending = [
            a
            for a in self.call("approver", "GET", "/approvals?status=PENDING")["items"]
            if a["run_id"] == run["id"]
        ]
        self.expect(len(pending) == 1, "the approver sees exactly this request")
        approval = pending[0]
        for step in approval["plan"]["steps"]:
            self.say(f"   plan: {step['key']:<12} {step['tool'] or '-':<22} {step['risk_level']}")
        path = f"/approvals/{approval['id']}"
        own = self.call("admin", "POST", f"{path}/approve", expect=403, json={})
        self.expect(
            "separation of duties" in own["error"]["message"],
            "the requester cannot approve their own request, even as admin",
        )
        self.call("operator", "POST", f"{path}/approve", expect=403, json={})
        self.expect(True, "an operator has no approval permission at all (403)")
        self.call("approver", "POST", f"{path}/reject", expect=422, json={})
        self.expect(True, "rejecting without a comment is refused (422)")
        self.call("approver", "POST", f"{path}/approve", json={"comment": "demo: looks fine"})
        self.wait_for("admin", run["id"], {"COMPLETED"})
        self.show_timeline("admin", run["id"])
        self.call("approver", "POST", f"{path}/approve", expect=409, json={})
        self.expect(True, "deciding twice is refused (409)")

    def role_limits(self) -> None:
        self.step("4. Roles are enforced by the API")
        self.call("viewer", "POST", "/workflows", expect=403, json={"goal": "Calculate 1+1"})
        self.expect(True, "a viewer cannot start workflows (403)")
        self.call("operator", "GET", "/users", expect=403)
        self.expect(True, "an operator cannot list users (403)")
        users = self.call("admin", "GET", "/users")["items"]
        self.expect(len(users) >= 4, f"an admin can ({len(users)} users)")

    def idempotency(self) -> None:
        self.step("5. A repeated request with the same Idempotency-Key does not repeat the work")
        key = f"demo-{uuid.uuid4().hex}"
        first = self.start("operator", "Calculate 2 + 2", headers={"Idempotency-Key": key})
        second = self.start("operator", "Calculate 2 + 2", headers={"Idempotency-Key": key})
        self.expect(first["id"] == second["id"], "the second call returned the same run")
        self.wait_for("operator", first["id"], {"COMPLETED"})

    def audit_trail(self) -> None:
        self.step("6. Everything above is in the append-only audit log")
        for action in ("workflow.created", "approval.approved", "access.denied"):
            page = self.call("admin", "GET", f"/audit-logs?action={action}&limit=1")
            self.expect(page["total"] > 0, f"{action}: {page['total']} entries")
        metrics = self.http.get("/metrics")
        if metrics.status_code == 200:
            runs = [ln for ln in metrics.text.splitlines() if ln.startswith("workflow_runs_total")]
            self.say("   metrics: " + "; ".join(runs[:3]))


def main() -> int:
    base_url = os.environ.get("API_BASE_URL", "http://localhost:8000")
    password = os.environ.get("SEED_DEMO_PASSWORD", DEFAULT_PASSWORD)
    try:
        with httpx.Client(base_url=base_url, timeout=30) as http:
            Demo(http, password).run()
    except (DemoError, httpx.HTTPError) as exc:
        print(f"\nDEMO FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
