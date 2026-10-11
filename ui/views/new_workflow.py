"""New-workflow form: describe a goal, start a run, jump to its live timeline."""

from __future__ import annotations

import uuid
from typing import Any

import streamlit as st

from ui import state
from ui.api_client import ApiClient, ApiError
from ui.format import plain

EXAMPLES = {
    "Answer from the knowledge base": "What is the refund window for digital goods?",
    "Calculate": "Calculate 1200 * 0.18 for the refund fee",
    "Open a ticket": "Check the SEV2 incident policy and open a ticket",
    "Needs approval (sends an email)": (
        "Check the SEV2 incident policy, open a ticket and email ops@example.com"
    ),
    "Remember a preference": "Remember that I prefer weekly summary reports",
    "Deep research": "Deep research on the refund policy for digital goods",
    "Compare": "Compare refund policy vs travel policy",
}
DEEP_EXAMPLES = frozenset({"Deep research", "Compare"})
GOAL_KEY = "new_goal"
IDEMPOTENCY_KEY = "new_workflow_key"
PENDING_KEY = "new_workflow_pending"  # a deep run waiting for "research again?" confirmation


def render(api: ApiClient) -> None:
    st.header("New workflow")
    caps = state.Capabilities.from_roles(api.roles)
    if not caps.can_create:
        st.info("Your role (viewer) can read workflows but not start them.")
        return

    st.caption("Try an example, then edit it:")
    columns = st.columns(len(EXAMPLES))
    for column, (name, text) in zip(columns, EXAMPLES.items(), strict=True):
        if column.button(name, key=f"example-{name}", width="stretch"):
            st.session_state[GOAL_KEY] = text
            st.session_state["new_deep"] = name in DEEP_EXAMPLES

    conversations = _conversations(api)
    with st.form("new-workflow"):
        goal = st.text_area(
            "Goal", key=GOAL_KEY, height=120, max_chars=4000,
            placeholder="What should the agents do?",
        )  # fmt: skip
        deep = st.checkbox(
            "Deep research mode",
            key="new_deep",
            help="Splits the goal into sub-questions, researches each in parallel "
            "(knowledge base, Wikipedia, arXiv, web) and writes a cited report.",
        )
        left, right = st.columns(2)
        revisions = left.slider(
            "Max review revisions", 0, 5, 2, help="Review can send the plan back"
        )
        chosen = right.selectbox(
            "Attach to conversation", ["(none)", *conversations], help="The answer is posted there"
        )
        submitted = st.form_submit_button("Start workflow", type="primary")
    if submitted:
        if len(goal.strip()) < 3:
            st.warning("Please describe the goal (at least 3 characters).")
            return
        params: dict[str, Any] = {
            "goal": goal.strip(),
            "conversation_id": conversations.get(chosen),
            "max_revisions": revisions,
            "mode": "deep" if deep else "standard",
        }
        earlier = _similar(api, params["goal"]) if deep else []
        if earlier:  # WHY only suggest: the user decides whether fresh research is worth it
            st.session_state[PENDING_KEY] = {"params": params, "earlier": earlier}
        else:
            st.session_state.pop(PENDING_KEY, None)
            _start(api, params)
    if pending := st.session_state.get(PENDING_KEY):
        _offer_earlier_research(api, pending)


def _similar(api: ApiClient, goal: str) -> list[dict[str, Any]]:
    try:
        return api.similar_runs(goal)
    except ApiError:
        return []  # a suggestion is optional; never block the form on it


def _offer_earlier_research(api: ApiClient, pending: dict[str, Any]) -> None:
    st.info("You already researched something similar. Open it, or research again.")
    for item in pending["earlier"]:
        left, right = st.columns([5, 1])
        left.markdown(f"**{plain(item['goal'])}** · {int(item['similarity'] * 100)}% similar")
        if right.button("Open", key=f"open-earlier-{item['id']}"):
            st.session_state.pop(PENDING_KEY, None)
            state.goto(state.PAGE_RUN, run_id=item["id"])
    if st.button("Research again anyway", key="research-again", type="primary"):
        st.session_state.pop(PENDING_KEY, None)
        _start(api, pending["params"])


def _start(api: ApiClient, params: dict[str, Any]) -> None:
    # WHY one key per form submit: a double click or a browser re-send creates only one run.
    key = st.session_state.setdefault(IDEMPOTENCY_KEY, uuid.uuid4().hex)
    try:
        run = api.create_workflow(
            params["goal"],
            conversation_id=params["conversation_id"],
            max_revisions=params["max_revisions"],
            mode=params["mode"],
            idempotency_key=key,
        )
    except ApiError as exc:
        st.session_state.pop(IDEMPOTENCY_KEY, None)
        st.error(exc.message)
        return
    st.session_state.pop(IDEMPOTENCY_KEY, None)
    state.goto(state.PAGE_RUN, run_id=run["id"])


def _conversations(api: ApiClient) -> dict[str, str]:
    """title -> id for the conversation picker (titles made unique with a short id)."""
    try:
        items = api.list_conversations()
    except ApiError:
        return {}
    return {f"{c['title']} · {c['id'][:6]}": c["id"] for c in items}
