"""End-to-end: the UI client and the real screens against a REAL running API (uvicorn).

What is real: the HTTP server, Postgres, auth, RBAC, the worker and the mock LLM. Nothing is
stubbed, so this proves the UI's idea of the API matches what the API actually serves.
Why a thread: uvicorn needs its own event loop; the sync test code (and Streamlit's AppTest)
talk to it over a real socket.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import DEMO_PASSWORD
from ui import state
from ui.api_client import ApiClient, ApiError

APP = str(Path(__file__).resolve().parents[2] / "ui" / "streamlit_app.py")


def signed_in(url: str, name: str) -> ApiClient:
    api = ApiClient(url)
    api.login(f"{name}@demo.local", DEMO_PASSWORD)
    return api


def wait_for(api: ApiClient, run_id: str, wanted: set[str], seconds: float = 30) -> dict:  # type: ignore[type-arg]
    deadline = time.monotonic() + seconds
    run = api.get_workflow(run_id)
    while run["status"] not in wanted and time.monotonic() < deadline:
        time.sleep(0.1)
        run = api.get_workflow(run_id)
    assert run["status"] in wanted, run["status"]
    return run


def test_client_session_and_error_envelope(live_api_url: str) -> None:
    api = ApiClient(live_api_url)
    with pytest.raises(ApiError) as bad:
        api.login("operator@demo.local", "wrong-password")
    assert bad.value.status == 401
    assert bad.value.request_id  # the real envelope carries a request id
    assert signed_in(live_api_url, "operator").roles == {"operator"}
    assert api.readiness()["status"] == "ready"


def test_full_workflow_through_the_client(live_api_url: str) -> None:
    operator = signed_in(live_api_url, "operator")
    run = operator.create_workflow("Calculate 1200 * 0.18", idempotency_key="ui-e2e-1")
    again = operator.create_workflow("Calculate 1200 * 0.18", idempotency_key="ui-e2e-1")
    assert again["id"] == run["id"]  # the Idempotency-Key header reached the API
    wait_for(operator, run["id"], {"COMPLETED"})
    timeline = operator.timeline(run["id"])
    assert timeline["totals"]["llm_calls"] > 0
    assert timeline["tool_executions"]
    assert any(r["id"] == run["id"] for r in operator.list_workflows())
    with pytest.raises(ApiError) as conflict:
        operator.cancel_workflow(run["id"])
    assert conflict.value.status == 409


def test_conversation_chat_and_documents(live_api_url: str) -> None:
    operator = signed_in(live_api_url, "operator")
    conversation = operator.create_conversation("UI e2e")
    run = operator.create_workflow(
        "What is the refund window for digital goods?", conversation_id=conversation["id"]
    )
    wait_for(operator, run["id"], {"COMPLETED"})
    deadline = time.monotonic() + 10
    roles: list[str] = []
    while "assistant" not in roles and time.monotonic() < deadline:
        roles = [m["role"] for m in operator.list_messages(conversation["id"])]
        time.sleep(0.1)
    assert roles[0] == "user"
    assert "assistant" in roles

    uploaded = operator.upload_document("UI note", "The cafeteria opens at nine every morning.")
    assert uploaded["created"] is True
    assert operator.search_documents("cafeteria")
    operator.delete_document(uploaded["document"]["id"])
    assert operator.upload_document("UI note", "Another note about lunch.")["created"] is True
    operator.archive_conversation(conversation["id"])


def test_approval_flow_and_separation_of_duties(live_api_url: str) -> None:
    operator = signed_in(live_api_url, "operator")
    approver = signed_in(live_api_url, "approver")
    run = operator.create_workflow(
        "Check the SEV2 incident policy, open a ticket and email ops@example.com",
        idempotency_key="ui-e2e-approval",
    )
    wait_for(operator, run["id"], {"AWAITING_APPROVAL"})
    (pending,) = [a for a in approver.list_approvals("PENDING") if a["run_id"] == run["id"]]
    assert pending["requester_email"] == "operator@demo.local"
    with pytest.raises(ApiError) as own:  # the requester may not decide their own request
        operator.approve(pending["id"])
    assert own.value.status == 403
    approver.approve(pending["id"], "looks fine")
    wait_for(operator, run["id"], {"COMPLETED"})


def test_admin_calls_and_permission_denials(live_api_url: str) -> None:
    admin = signed_in(live_api_url, "admin")
    users = admin.list_users()
    assert {"admin@demo.local", "viewer@demo.local"} <= {u["email"] for u in users}
    assert admin.audit_logs(action="auth.login", limit=5)["total"] > 0
    viewer = signed_in(live_api_url, "viewer")
    with pytest.raises(ApiError) as denied:
        viewer.list_users()
    assert denied.value.status == 403
    with pytest.raises(ApiError) as cannot_create:
        viewer.create_workflow("Calculate 1+1")
    assert cannot_create.value.status == 403


def test_metrics_endpoint_is_parseable(live_api_url: str) -> None:
    from ui.format import parse_prometheus, total

    samples = parse_prometheus(signed_in(live_api_url, "viewer").metrics_text())
    assert total(samples, "http_requests_total") > 0


def test_screens_work_against_the_real_api(live_api_url: str) -> None:
    """Drive the actual Streamlit pages (login, new workflow -> timeline, approvals, admin)."""
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["api"] = ApiClient(live_api_url)
    at.run()
    at.text_input[0].set_value("operator@demo.local")
    at.text_input[1].set_value(DEMO_PASSWORD)
    at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.sidebar.radio(key=state.NAV_KEY).value == state.PAGE_CHAT

    at.sidebar.radio(key=state.NAV_KEY).set_value(state.PAGE_NEW).run()
    at.text_area(key="new_goal").set_value("Calculate 7 * 6")
    (submit,) = [b for b in at.main.button if b.label == "Start workflow"]
    submit.click().run()
    assert at.sidebar.radio(key=state.NAV_KEY).value == state.PAGE_RUN
    run_id = at.session_state[state.SELECTED_RUN_KEY]
    wait_for(at.session_state["api"], run_id, {"COMPLETED"})
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert {m.label for m in at.metric} >= {"LLM calls", "Cost", "Tool calls"}
    assert at.success  # the run summary

    at.sidebar.radio(key=state.NAV_KEY).set_value(state.PAGE_METRICS).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(m.label == "HTTP requests" for m in at.metric)

    admin = AppTest.from_file(APP, default_timeout=30)
    admin.session_state["api"] = signed_in(live_api_url, "admin")
    admin.session_state[state.NAV_KEY] = state.PAGE_ADMIN
    admin.run()
    assert not admin.exception, [e.value for e in admin.exception]
    assert admin.multiselect  # one roles picker per user
