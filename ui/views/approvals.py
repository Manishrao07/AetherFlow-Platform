"""Approvals inbox: see exactly what a plan will do, then approve or reject with a comment.

Separation of duties: the API refuses a requester deciding their own run (and the database
enforces it too). The screen mirrors that by hiding the buttons for your own requests, so nobody
clicks something that can only fail.
"""

from __future__ import annotations

import json

import streamlit as st

from ui import state
from ui.api_client import ApiClient, ApiError
from ui.format import badge, plain, short_time


def render(api: ApiClient) -> None:
    st.header("Approvals")
    caps = state.Capabilities.from_roles(api.roles)
    if not caps.can_decide:
        st.info(
            "Only approvers and admins can decide approvals. Below are the requests "
            "waiting on a decision for your own workflows."
        )
    status = st.radio("Show", ["PENDING", "APPROVED", "REJECTED", "EXPIRED"], horizontal=True)
    approvals = api.list_approvals(status)
    if not approvals:
        st.success(f"No {status.lower()} approvals.") if status == "PENDING" else st.caption(
            f"No {status.lower()} approvals."
        )
        return
    for approval in approvals:
        _card(api, approval, caps)


def _card(api: ApiClient, approval: dict, caps: state.Capabilities) -> None:  # type: ignore[type-arg]
    title = f"{approval['risk_level']} risk · {(approval.get('goal') or '')[:80]}"
    with st.container(border=True):
        st.markdown(f"**{plain(title)}**  {badge(approval['status'])}")
        expires = short_time(approval["expires_at"])
        st.caption(f"Requested by {plain(approval['requester_email'])} · expires {expires}")
        st.markdown(plain(approval["reason"]))
        steps = (approval.get("plan") or {}).get("steps", [])
        if steps:
            st.dataframe(
                [
                    {
                        "step": s["key"],
                        "what": s["description"],
                        "tool": s.get("tool") or "-",
                        "risk": s["risk_level"],
                        "needs approval": "yes" if s["requires_approval"] else "",
                    }
                    for s in steps
                ],
                hide_index=True,
                width="stretch",
            )
            with st.expander("Exact inputs the plan will use"):
                st.code(
                    json.dumps([{s["key"]: s["inputs"]} for s in steps], indent=2), language="json"
                )
        if st.button("View run", key=f"view-{approval['id']}"):
            state.goto(state.PAGE_RUN, run_id=approval["run_id"])
        if approval["status"] != "PENDING":
            if approval.get("comment"):
                st.caption(f"Comment: {plain(approval['comment'])}")
            return
        if approval["requester_email"] == api.email:
            st.info("You requested this, so someone else has to decide it (separation of duties).")
            return
        if not caps.can_decide:
            return
        comment = st.text_input("Comment (required to reject)", key=f"comment-{approval['id']}")
        approve_col, reject_col, _ = st.columns([1, 1, 4])
        if approve_col.button("Approve", key=f"approve-{approval['id']}", type="primary"):
            _decide(lambda: api.approve(approval["id"], comment.strip() or None), "Approved")
        if reject_col.button("Reject", key=f"reject-{approval['id']}"):
            if not comment.strip():
                st.warning("Please explain the rejection in the comment.")
            else:
                _decide(lambda: api.reject(approval["id"], comment.strip()), "Rejected")


def _decide(action, done: str) -> None:  # type: ignore[no-untyped-def]
    try:
        action()
    except ApiError as exc:
        st.error(exc.message)
        return
    st.toast(f"{done}.")
    st.rerun()
