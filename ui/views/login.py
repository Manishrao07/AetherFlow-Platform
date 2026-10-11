"""Sign-in screen."""

from __future__ import annotations

import os

import streamlit as st

from ui.api_client import ApiClient, ApiError

NOTICE_KEY = "login_notice"
DEMO_USERS = ("admin", "approver", "operator", "viewer")


def render(api: ApiClient) -> None:
    _, middle, _ = st.columns([1, 1.2, 1])
    with middle:
        st.title("🧭 Workflow Platform")
        st.caption("Planner · Retrieval · Execution · Review agents with human approvals")
        notice = st.session_state.pop(NOTICE_KEY, None)
        if notice:
            st.warning(notice)
        with st.form("login"):
            email = st.text_input("Email", placeholder="operator@demo.local")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
        if submitted:
            try:
                api.login(email.strip(), password)
            except ApiError as exc:
                st.error(exc.message)
            else:
                st.rerun()
        # WHY opt-in: demo accounts are a convenience for local runs, never shown by default.
        if os.environ.get("UI_SHOW_DEMO_HINT", "").lower() in {"1", "true", "yes"}:
            with st.expander("Demo accounts"):
                st.write(
                    "One account per role: "
                    + ", ".join(f"`{name}@demo.local`" for name in DEMO_USERS)
                    + ". The password is `SEED_DEMO_PASSWORD` (see the README)."
                )
