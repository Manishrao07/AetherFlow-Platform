"""Navigation and session helpers shared by the screens.

What: the list of pages, which roles see which, and ``goto`` for jumping to a page (for example
"open this run's timeline" after starting a workflow).
Why our own tiny router instead of ``st.navigation``: pages need role-based visibility and
programmatic jumps carrying data (a run id); a radio in the sidebar plus a ``goto`` flag does that
in a few lines, works the same in tests (``AppTest``) and is easy to explain.
Roles are used here ONLY to hide screens and disable buttons that would be refused anyway. The
API enforces every permission; hiding is a courtesy, not security.
"""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

PAGE_CHAT = "Chat"
PAGE_NEW = "New workflow"
PAGE_RUN = "Run timeline"
PAGE_APPROVALS = "Approvals"
PAGE_DOCS = "Documents"
PAGE_METRICS = "Metrics"
PAGE_ADMIN = "Admin"

NAV_KEY = "nav_page"
GOTO_KEY = "_goto_page"
SELECTED_RUN_KEY = "selected_run_id"


@dataclass(frozen=True)
class Capabilities:
    """What the signed-in user can do, derived from their roles."""

    can_create: bool
    can_decide: bool
    is_admin: bool

    @classmethod
    def from_roles(cls, roles: frozenset[str]) -> Capabilities:
        return cls(
            can_create=bool(roles & {"operator", "admin"}),
            can_decide=bool(roles & {"approver", "admin"}),
            is_admin="admin" in roles,
        )


def visible_pages(caps: Capabilities) -> list[str]:
    pages = [PAGE_CHAT, PAGE_NEW, PAGE_RUN, PAGE_APPROVALS, PAGE_DOCS, PAGE_METRICS]
    if caps.is_admin:
        pages.append(PAGE_ADMIN)
    return pages


def goto(page: str, *, run_id: str | None = None) -> None:
    """Open ``page`` on the next run of the script (optionally focused on one workflow run)."""
    st.session_state[GOTO_KEY] = page
    if run_id is not None:
        st.session_state[SELECTED_RUN_KEY] = run_id
    st.rerun()


def apply_pending_goto() -> None:
    """Called at the top of the script, BEFORE the sidebar radio is created.

    WHY: Streamlit forbids changing a widget's value after the widget exists in the same run.
    """
    target = st.session_state.pop(GOTO_KEY, None)
    if target:
        st.session_state[NAV_KEY] = target
