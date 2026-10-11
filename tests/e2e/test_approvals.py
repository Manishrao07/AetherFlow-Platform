"""Human-in-the-loop: pause for approval, separation of duties, rejection, expiry."""

import asyncio

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Approval, AuditLog, NotificationOutbox, Ticket, ToolExecution
from tests.e2e.helpers import (
    as_user,
    flow_app,
    start,
    status_path,
    step_by_key,
    timeline,
    wait_for,
    wait_status,
)

GOAL = "Check the SEV2 incident policy, open a ticket and email ops@example.com"


async def pending(http, headers):
    body = (await http.get("/api/v1/approvals", headers=headers)).json()
    return body["items"]


async def count(session_factory, model) -> int:
    async with session_factory() as s:
        return int(await s.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


async def test_high_risk_plan_pauses_then_runs_after_approval(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op, approver = await as_user(http, "operator"), await as_user(http, "approver")
        run = await start(http, op, GOAL)
        paused = await wait_status(http, op, run["id"], "AWAITING_APPROVAL")
        assert paused["plan"]["steps"][2]["requires_approval"] is True
        assert await count(session_factory, NotificationOutbox) == 0  # nothing has run yet

        inbox = await pending(http, approver)
        assert len(inbox) == 1 and inbox[0]["run_id"] == run["id"]
        assert (
            inbox[0]["risk_level"] == "HIGH"
            and inbox[0]["requester_email"] == "operator@demo.local"
        )
        assert inbox[0]["goal"] == GOAL and inbox[0]["plan"]["steps"]  # approver sees what will run
        assert "send_notification" in inbox[0]["reason"]
        assert await pending(http, op) == inbox  # the requester can see their own request

        decided = await http.post(
            f"/api/v1/approvals/{inbox[0]['id']}/approve",
            json={"comment": "Recipients check out"},
            headers=approver,
        )
        assert decided.status_code == 200 and decided.json()["status"] == "APPROVED"
        done = await wait_status(http, op, run["id"], "COMPLETED")
        assert done["review_verdict"] == "APPROVED"

        tl = await timeline(http, op, run["id"])
        assert status_path(tl) == [
            "PLANNING",
            "AWAITING_APPROVAL",
            "EXECUTING",
            "REVIEWING",
            "COMPLETED",
        ]
        assert step_by_key(tl, "s3")["output"]["output"]["status"] == "sent (mock)"
        assert tl["approvals"][0]["comment"] == "Recipients check out"
        assert await count(session_factory, NotificationOutbox) == 1
        async with session_factory() as s:
            ticket = await s.scalar(sa.select(Ticket))
            actions = (
                await s.scalars(
                    sa.select(AuditLog.action).where(
                        AuditLog.actor_email.in_(["approver@demo.local"])
                    )
                )
            ).all()
        assert ticket is not None and ticket.severity == "SEV2"
        assert (
            "{{" not in ticket.description and "knowledge base" in ticket.description
        )  # {{s1}} replaced by the cited answer
        assert "approval.approved" in actions
        assert await pending(http, approver) == []


async def test_requester_cannot_approve_their_own_run_even_as_admin(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        admin = await as_user(http, "admin")
        run = await start(http, admin, GOAL)
        await wait_status(http, admin, run["id"], "AWAITING_APPROVAL")
        approval_id = (await pending(http, admin))[0]["id"]
        response = await http.post(
            f"/api/v1/approvals/{approval_id}/approve", json={}, headers=admin
        )
        assert (
            response.status_code == 403
            and "separation of duties" in response.json()["error"]["message"]
        )
        # another admin-capable person (the approver) can
        approver = await as_user(http, "approver")
        assert (
            await http.post(f"/api/v1/approvals/{approval_id}/approve", json={}, headers=approver)
        ).status_code == 200
        await wait_status(http, admin, run["id"], "COMPLETED")


async def test_database_itself_blocks_self_approval(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "AWAITING_APPROVAL")
    async with session_factory() as s:
        approval = await s.scalar(sa.select(Approval))
        assert approval is not None
        try:
            await s.execute(
                sa.update(Approval)
                .where(Approval.id == approval.id)
                .values(
                    status="APPROVED", decided_by=approval.requested_by, decided_at=sa.func.now()
                )
            )
            raised = False
        except sa.exc.IntegrityError:
            raised = True
    assert raised


async def test_only_approvers_and_admins_can_decide(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "AWAITING_APPROVAL")
        approval_id = (await pending(http, op))[0]["id"]
        for who in ("operator", "viewer"):
            headers = await as_user(http, who)
            response = await http.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=headers
            )
            assert response.status_code == 403
        assert (
            await http.get(
                f"/api/v1/approvals/{approval_id}", headers=await as_user(http, "viewer")
            )
        ).status_code == 404
        assert (await pending(http, await as_user(http, "viewer"))) == []
        run_now = await http.get(f"/api/v1/workflows/{run['id']}", headers=op)
        assert run_now.json()["status"] == "AWAITING_APPROVAL"


async def test_rejection_cancels_the_run_and_nothing_executes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op, approver = await as_user(http, "operator"), await as_user(http, "approver")
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "AWAITING_APPROVAL")
        approval_id = (await pending(http, approver))[0]["id"]
        blank = await http.post(
            f"/api/v1/approvals/{approval_id}/reject", json={}, headers=approver
        )
        assert blank.status_code == 422  # a reason is mandatory
        rejected = await http.post(
            f"/api/v1/approvals/{approval_id}/reject",
            json={"comment": "Wrong recipient"},
            headers=approver,
        )
        assert rejected.json()["status"] == "REJECTED"
        cancelled = await wait_status(http, op, run["id"], "CANCELLED")
        assert "Wrong recipient" in cancelled["error"]
        assert await count(session_factory, ToolExecution) == 0
        assert await count(session_factory, NotificationOutbox) == 0
        assert await count(session_factory, Ticket) == 0


async def test_an_approval_can_only_be_decided_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op, approver, admin = [await as_user(http, n) for n in ("operator", "approver", "admin")]
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "AWAITING_APPROVAL")
        approval_id = (await pending(http, approver))[0]["id"]
        url = f"/api/v1/approvals/{approval_id}"
        results = await asyncio.gather(
            http.post(f"{url}/approve", json={}, headers=approver),
            http.post(f"{url}/reject", json={"comment": "no"}, headers=admin),
        )
        assert sorted(r.status_code for r in results) == [200, 409]
        again = await http.post(f"{url}/approve", json={}, headers=approver)
        assert again.status_code == 409 and "already" in again.json()["error"]["message"]
        await wait_status(http, op, run["id"], "COMPLETED", "CANCELLED")


async def test_unanswered_approval_expires_and_fails_the_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, approval_ttl_seconds=1) as http:
        op, approver = await as_user(http, "operator"), await as_user(http, "approver")
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "AWAITING_APPROVAL")
        approval_id = (await pending(http, approver))[0]["id"]
        await asyncio.sleep(1.2)
        late = await http.post(
            f"/api/v1/approvals/{approval_id}/approve", json={}, headers=approver
        )
        assert late.status_code == 409 and "expired" in late.json()["error"]["message"]
        failed = await wait_status(http, op, run["id"], "FAILED")
        assert "expired" in failed["error"]
        listing = await http.get("/api/v1/approvals?status=EXPIRED", headers=approver)
        assert listing.json()["total"] == 1
        assert await count(session_factory, NotificationOutbox) == 0


async def test_sweeper_expires_approvals_nobody_touched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory, approval_ttl_seconds=1) as http:
        op = await as_user(http, "operator")
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "AWAITING_APPROVAL")
        await asyncio.sleep(1.1)
        await http.app.state.worker.sweep()  # type: ignore[attr-defined]
        await wait_status(http, op, run["id"], "FAILED")


async def test_cancelling_a_waiting_run_closes_its_approval(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with flow_app(session_factory) as http:
        op, approver = await as_user(http, "operator"), await as_user(http, "approver")
        run = await start(http, op, GOAL)
        await wait_status(http, op, run["id"], "AWAITING_APPROVAL")
        approval_id = (await pending(http, approver))[0]["id"]
        assert (await http.post(f"/api/v1/workflows/{run['id']}/cancel", headers=op)).json()[
            "status"
        ] == "CANCELLED"
        assert await pending(http, approver) == []
        late = await http.post(
            f"/api/v1/approvals/{approval_id}/approve", json={}, headers=approver
        )
        assert late.status_code == 409
        await wait_for(lambda: _idle(http), what="worker idle")


async def _idle(http) -> bool:
    worker = http.app.state.worker
    return not worker._active and worker._queue.empty()
