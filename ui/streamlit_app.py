"""Streamlit entry point: sign-in, sidebar navigation and the page router.

Run locally:  ``streamlit run ui/streamlit_app.py``  (API at ``API_BASE_URL``, default
http://localhost:8000). In Docker the ``ui`` service points at ``http://api:8000``.
Everything goes through the REST API; there is no database access in this folder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# WHY: ``streamlit run ui/streamlit_app.py`` puts only ``ui/`` on sys.path, but the modules
# import each other as ``ui.xxx``; add the project root so it works from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st

from ui import state
from ui.api_client import ApiClient, ApiError
from ui.views import (
    admin,
    approvals,
    chat,
    documents,
    login,
    metrics,
    new_workflow,
    run_timeline,
)

VIEWS = {
    state.PAGE_CHAT: chat.render,
    state.PAGE_NEW: new_workflow.render,
    state.PAGE_RUN: run_timeline.render,
    state.PAGE_APPROVALS: approvals.render,
    state.PAGE_DOCS: documents.render,
    state.PAGE_METRICS: metrics.render,
    state.PAGE_ADMIN: admin.render,
}


def get_api() -> ApiClient:
    """One client per browser session (holds that user's tokens)."""
    if "api" not in st.session_state:
        st.session_state["api"] = ApiClient(os.environ.get("API_BASE_URL", "http://localhost:8000"))
    api: ApiClient = st.session_state["api"]
    return api


def sign_out(api: ApiClient, notice: str | None = None) -> None:
    """Forget the session and show the login screen (optionally explaining why)."""
    api.logout()
    for key in list(st.session_state.keys()):
        del st.session_state[key]  # also forgets selected run, drafts, cached lists
    if notice:
        # WHY: the rerun below discards anything drawn now; the login screen shows this instead.
        st.session_state[login.NOTICE_KEY] = notice
    st.rerun()


def main() -> None:
    st.set_page_config(page_title="Workflow Platform", page_icon="🧭", layout="wide")
    api = get_api()
    if not api.is_authenticated:
        login.render(api)
        return

    caps = state.Capabilities.from_roles(api.roles)
    pages = state.visible_pages(caps)
    state.apply_pending_goto()
    with st.sidebar:
        st.markdown("### 🧭 Workflow Platform")
        st.caption(f"{api.email}  \n{', '.join(sorted(api.roles))}")
        page = st.radio("Navigate", pages, key=state.NAV_KEY, label_visibility="collapsed")
        st.divider()
        if st.button("Sign out", width="stretch"):
            sign_out(api)
    try:
        VIEWS[page](api)
    except ApiError as exc:
        if exc.code == "session_expired":
            sign_out(api, exc.message)
        else:
            # WHY a catch-all here: a screen should never show a stack trace for an API refusal.
            st.error(str(exc))


main()
