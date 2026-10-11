"""Live run timeline: plan and step status, tool calls, LLM calls (tokens, cost), events.

Live updates: while the run is active the body is a Streamlit *fragment* that re-fetches the
timeline every 2 seconds (only that part of the page reruns). When the run reaches a final state
the whole page reruns once, which switches the auto-refresh off.
Why polling instead of the SSE endpoint: Streamlit scripts are request/response; polling a
cheap read endpoint is simple, reliable behind proxies, and the API already supports SSE for
other clients (``GET /workflows/{id}/events``).
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from ui import state
from ui.api_client import ApiClient, ApiError
from ui.format import (
    badge,
    duration,
    event_line,
    is_final,
    llm_table,
    money,
    plain,
    report_markdown,
    steps_table,
    tool_table,
)

REFRESH_SECONDS = 2


def render(api: ApiClient) -> None:
    st.header("Run timeline")
    runs = api.list_workflows(limit=30)
    run_id = _pick_run(runs)
    if run_id is None:
        st.info("No workflow runs yet. Start one from **New workflow** or **Chat**.")
        return

    first = api.timeline(run_id)
    live = not is_final(first["run"]["status"])
    st.session_state["_run_was_live"] = live
    # WHY the call style: the refresh interval depends on the run being active right now.
    body = st.fragment(_body, run_every=REFRESH_SECONDS if live else None)
    body(api, run_id)


def _pick_run(runs: list[dict[str, Any]]) -> str | None:
    selected = st.session_state.get(state.SELECTED_RUN_KEY)
    options = {r["id"]: r for r in runs}
    if selected and selected not in options:
        options = {selected: {"id": selected, "goal": "(selected run)", "status": "?"}, **options}
    if not options:
        return None
    ids = list(options)
    index = ids.index(selected) if selected in options else 0
    chosen = st.selectbox(
        "Run",
        ids,
        index=index,
        format_func=lambda i: f"{options[i]['status']:<18} {options[i]['goal'][:70]}",
        key="run_pick",
    )
    st.session_state[state.SELECTED_RUN_KEY] = chosen
    return str(chosen)


def _body(api: ApiClient, run_id: str) -> None:
    try:
        tl = api.timeline(run_id)
    except ApiError as exc:
        st.error(exc.message)
        return
    run = tl["run"]
    if st.session_state.get("_run_was_live") and is_final(run["status"]):
        st.session_state["_run_was_live"] = False
        st.rerun()  # leave the auto-refreshing fragment

    _header(run, tl["totals"])
    render_report_tools(api, run)
    _actions(api, tl)
    _plan(tl)
    _calls(tl)
    _approvals(tl)
    _events(tl)


def _header(run: dict[str, Any], totals: dict[str, Any]) -> None:
    st.markdown(f"### {plain(run['goal'])}")
    st.markdown(
        f"{badge(run['status'])}  review: {badge(run['review_verdict'])}  "
        f"score: **{run['review_score'] if run['review_score'] is not None else '-'}**  "
        f"revisions: **{run['revision_count']}/{run['max_revisions']}**  "
        f"elapsed (incl. any approval wait): **{duration(run['started_at'], run['finished_at'])}**"
    )
    if run["error"]:
        st.error(plain(run["error"]))
    result = run.get("result") or {}
    if run["status"] == "COMPLETED" and result.get("confidence"):
        _report(run, result)
    elif run["status"] == "COMPLETED" and result.get("summary"):
        st.success(plain(result["summary"]))
    a, b, c, d = st.columns(4)
    a.metric("LLM calls", totals.get("llm_calls", 0))
    b.metric(
        "Tokens in / out",
        f"{totals.get('prompt_tokens', 0)} / {totals.get('completion_tokens', 0)}",
    )
    c.metric("Cost", money(totals.get("cost_usd", 0)))
    d.metric("Tool calls", totals.get("tool_executions", 0))


def _report(run: dict[str, Any], result: dict[str, Any]) -> None:
    """A finished research report: confidence, the text, downloads and follow-up questions."""
    confidence = result["confidence"]
    a, b, c = st.columns(3)
    a.metric("Confidence", f"{confidence['label']} ({confidence['score']:.2f})")
    b.metric("Sources cited", confidence["sources"])
    c.metric("Questions answered", f"{int(float(confidence['coverage']) * 100)}%")
    with st.container(border=True):
        st.markdown(report_markdown(result["summary"]))


def render_report_tools(api: ApiClient, run: dict[str, Any]) -> None:
    """Download buttons and the follow-up box (needs the API, so kept apart from ``_report``)."""
    result = run.get("result") or {}
    if run["status"] != "COMPLETED" or not result.get("summary"):
        return
    caps = state.Capabilities.from_roles(api.roles)
    left, right, _ = st.columns([1, 1, 3])
    try:
        left.download_button(
            "Download PDF", api.download_report(run["id"], "pdf"),
            file_name=f"report-{run['id'][:8]}.pdf", mime="application/pdf", key="dl-pdf",
        )  # fmt: skip
        right.download_button(
            "Download Markdown", api.download_report(run["id"], "md"),
            file_name=f"report-{run['id'][:8]}.md", mime="text/markdown", key="dl-md",
        )  # fmt: skip
    except ApiError as exc:
        st.warning(exc.message)
    if not (caps.can_create and result.get("confidence")):
        return
    st.subheader("Ask a follow-up")
    st.caption("Answered from this report only - no new research.")
    question = st.text_input("Question", key=f"ask-{run['id']}", max_chars=500)
    if st.button("Ask", key="ask-followup") and len(question.strip()) >= 3:
        try:
            reply = api.ask_followup(run["id"], question.strip())
        except ApiError as exc:
            st.error(exc.message)
        else:
            sources = "; ".join(f"[{c['n']}] {plain(c['citation'])}" for c in reply["citations"])
            st.markdown(report_markdown(reply["answer"]))
            if sources:
                st.caption(f"Sources: {sources}")


def _actions(api: ApiClient, tl: dict[str, Any]) -> None:
    run = tl["run"]
    caps = state.Capabilities.from_roles(api.roles)
    left, right, _ = st.columns([1, 1, 3])
    if not is_final(run["status"]) and left.button("Cancel run", key="cancel-run"):
        try:
            api.cancel_workflow(run["id"])
        except ApiError as exc:
            st.error(exc.message)
        else:
            st.rerun()
    failed = [
        s for s in tl["steps"] if s["status"] == "FAILED" and s["revision"] == run["revision_count"]
    ]
    if run["status"] == "FAILED" and failed and (caps.can_create):
        step = failed[0]
        if right.button(f"Retry step {step['step_key']}", key="retry-step"):
            try:
                api.retry_step(run["id"], step["id"])
            except ApiError as exc:
                st.error(exc.message)
            else:
                st.rerun()


def _plan(tl: dict[str, Any]) -> None:
    run = tl["run"]
    revisions = sorted({s["revision"] for s in tl["steps"]}, reverse=True)
    st.subheader("Plan and steps")
    if run["plan"] and run["plan"].get("summary"):
        st.caption(plain(run["plan"]["summary"]))
    if not revisions:
        st.write("Waiting for the plan…")
        return
    for revision in revisions:
        title = f"Revision {revision}" + (" (current)" if revision == run["revision_count"] else "")
        with st.expander(title, expanded=revision == revisions[0]):
            st.dataframe(steps_table(tl["steps"], revision), hide_index=True, width="stretch")
            for step in (s for s in tl["steps"] if s["revision"] == revision):
                with st.expander(
                    f"{step['step_key']} · {step['tool'] or step['agent']} · {step['status']}"
                ):
                    if step["error"]:
                        st.error(plain(step["error"]))
                    st.caption("Inputs")
                    st.code(json.dumps(step["inputs"], indent=2), language="json")
                    if step["output"] is not None:
                        st.caption("Output")
                        st.code(json.dumps(step["output"], indent=2)[:4000], language="json")


def _calls(tl: dict[str, Any]) -> None:
    keys = {s["id"]: s["step_key"] for s in tl["steps"]}
    left, right = st.columns(2)
    with left:
        st.subheader("Tool calls")
        if tl["tool_executions"]:
            st.dataframe(tool_table(tl["tool_executions"], keys), hide_index=True, width="stretch")
        else:
            st.caption("None yet.")
    with right:
        st.subheader("LLM calls")
        if tl["llm_calls"]:
            st.dataframe(llm_table(tl["llm_calls"]), hide_index=True, width="stretch")
        else:
            st.caption("None yet.")


def _approvals(tl: dict[str, Any]) -> None:
    if not tl["approvals"]:
        return
    st.subheader("Approvals")
    for approval in tl["approvals"]:
        who = plain(approval["requester_email"])
        st.markdown(
            f"{badge(approval['status'])} {approval['risk_level']} risk · requested by **{who}** · "
            f"{plain(approval['reason'])}"
            + (f" · comment: _{plain(approval['comment'])}_" if approval["comment"] else "")
        )
    if tl["run"]["status"] == "AWAITING_APPROVAL" and st.button(
        "Open approvals inbox", key="goto-approvals"
    ):
        state.goto(state.PAGE_APPROVALS)


def _events(tl: dict[str, Any]) -> None:
    with st.expander(f"Event log ({len(tl['events'])})"):
        for event in tl["events"]:
            st.markdown(event_line(event))
