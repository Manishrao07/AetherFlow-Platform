"""Chat: a conversation whose messages can start workflows, with the answer posted back.

How it works: the user's message is sent as a workflow GOAL attached to the conversation. The
platform stores it as the user's message and, when the run is approved by the Review agent, posts
the answer as the assistant's message. The screen waits for the run (up to ``MAX_WAIT_SECONDS``)
and stops early if a human has to act (approval), pointing to the right screen.
"""

from __future__ import annotations

import time
from typing import Any

import streamlit as st

from ui import state
from ui.api_client import ApiClient, ApiError
from ui.format import PAUSED_RUN_STATUSES, badge, is_final, label, plain

MAX_WAIT_SECONDS = 90.0
POLL_SECONDS = 0.8
OUTCOME_KEY = "chat_outcome"


def render(api: ApiClient) -> None:
    st.header("Chat")
    caps = state.Capabilities.from_roles(api.roles)
    conversations = api.list_conversations()
    selected = _sidebar(api, conversations)
    if selected is None:
        st.info("Create a conversation to start chatting with the agents.")
        return

    for message in api.list_messages(selected["id"]):
        with st.chat_message("user" if message["role"] == "user" else "assistant"):
            st.markdown(plain(message["content"]))
            if message["run_id"] and st.button("Open run", key=f"run-{message['id']}"):
                state.goto(state.PAGE_RUN, run_id=message["run_id"])

    if not caps.can_create:
        st.caption("Viewers can read conversations but not send messages.")
        return
    _outcome()
    prompt = st.chat_input("Ask or instruct the agents…")
    if prompt:
        _ask(api, selected["id"], prompt)


def _sidebar(api: ApiClient, conversations: list[dict[str, Any]]) -> dict[str, Any] | None:
    with st.sidebar, st.expander("New conversation", expanded=not conversations):
        title = st.text_input("Title", key="new_conversation_title")
        if st.button("Create", key="create_conversation") and title.strip():
            try:
                created = api.create_conversation(title.strip())
            except ApiError as exc:
                st.error(exc.message)
            else:
                st.session_state["chat_selected"] = created["id"]
                st.rerun()
    if not conversations:
        return None
    by_id = {c["id"]: c for c in conversations}
    ids = list(by_id)
    default = st.session_state.get("chat_selected")
    index = ids.index(default) if default in by_id else 0
    chosen = st.selectbox("Conversation", ids, index=index, format_func=lambda i: by_id[i]["title"])
    st.session_state["chat_selected"] = chosen
    if st.button("Archive conversation", key="archive_conversation"):
        api.archive_conversation(chosen)
        st.session_state.pop("chat_selected", None)
        st.rerun()
    return by_id[chosen]


def _ask(api: ApiClient, conversation_id: str, prompt: str) -> None:
    """Send the prompt as a workflow, wait for it, then rerun so the history shows the reply.

    WHY rerun + stored outcome: buttons drawn inside this one-shot handler would vanish on the
    next run (``chat_input`` returns None again), so clicking them would do nothing. The outcome
    that needs a human (approval, failure, still running) is kept in session state and drawn by
    ``_outcome`` on every run instead.
    """
    st.session_state.pop(OUTCOME_KEY, None)
    with st.chat_message("user"):
        st.markdown(plain(prompt))
    try:
        run = api.create_workflow(prompt, conversation_id=conversation_id)
    except ApiError as exc:
        st.error(exc.message)
        return
    with st.chat_message("assistant"):
        final = _wait(api, run["id"])
    status = final["status"]
    if status != "COMPLETED":  # the answer of a completed run is in the history after the rerun
        st.session_state[OUTCOME_KEY] = {
            "run_id": run["id"],
            "status": status,
            "error": final.get("error"),
        }
    st.rerun()


def _outcome() -> None:
    """What happened to the last message when it did not simply complete."""
    outcome = st.session_state.get(OUTCOME_KEY)
    if not outcome:
        return
    status = outcome["status"]
    with st.chat_message("assistant"):
        if status in PAUSED_RUN_STATUSES:
            st.warning("This plan includes a high-risk action and needs a human approval first.")
            if st.button("Go to approvals", key="chat-go-approvals"):
                state.goto(state.PAGE_APPROVALS)
        elif is_final(status):
            st.error(plain(outcome.get("error")) or f"The run ended as {status}.")
        else:
            st.info(f"Still working ({status}). Follow it in the run timeline.")
        st.caption(f"Run {outcome['run_id'][:8]} · {badge(status)}")
        if st.button("Open run timeline", key="chat-open-run"):
            state.goto(state.PAGE_RUN, run_id=outcome["run_id"])


def _wait(api: ApiClient, run_id: str) -> dict[str, Any]:
    """Poll the run, showing its status, until it is final, paused for a human, or we give up."""
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    with st.status("Working on it…", expanded=False) as box:
        while True:
            run = api.get_workflow(run_id)
            box.update(label=f"{label(run['status'])}")
            if is_final(run["status"]) or run["status"] in PAUSED_RUN_STATUSES:
                box.update(state="complete" if run["status"] == "COMPLETED" else "error")
                return run
            if time.monotonic() > deadline:
                return run
            time.sleep(POLL_SECONDS)
