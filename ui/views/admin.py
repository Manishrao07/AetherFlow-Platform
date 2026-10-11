"""Admin: manage users and roles, browse the append-only audit log."""

from __future__ import annotations

import streamlit as st

from ui import state
from ui.api_client import ApiClient, ApiError
from ui.format import plain, short_time

ROLES = ["admin", "approver", "operator", "viewer"]
PAGE_SIZE = 25


def render(api: ApiClient) -> None:
    st.header("Admin")
    if not state.Capabilities.from_roles(api.roles).is_admin:
        st.error("Admins only.")
        return
    users_tab, audit_tab = st.tabs(["Users", "Audit log"])
    with users_tab:
        _users(api)
    with audit_tab:
        _audit(api)


def _users(api: ApiClient) -> None:
    for user in api.list_users():
        with st.container(border=True):
            left, middle, right = st.columns([3, 3, 1])
            left.markdown(f"**{plain(user['full_name'])}**  \n{plain(user['email'])}")
            roles = middle.multiselect(
                "Roles", ROLES, default=user["roles"], key=f"roles-{user['id']}",
                label_visibility="collapsed",
            )  # fmt: skip
            active = right.toggle("Active", value=user["is_active"], key=f"active-{user['id']}")
            changed = bool(roles) and sorted(roles) != sorted(user["roles"])
            if changed and st.button("Save roles", key=f"save-roles-{user['id']}"):
                _apply(lambda u=user, r=roles: api.set_roles(u["id"], r), "Roles updated")
            if active != user["is_active"]:
                _apply(lambda u=user, a=active: api.set_active(u["id"], a), "Account updated")


def _apply(action, done: str) -> None:  # type: ignore[no-untyped-def]
    try:
        action()
    except ApiError as exc:
        st.error(exc.message)
        return
    st.toast(done)
    st.rerun()


def _audit(api: ApiClient) -> None:
    left, right, third = st.columns(3)
    action = left.text_input(
        "Action contains / equals", key="audit_action", placeholder="approval.approved"
    )
    resource = right.text_input("Resource type", key="audit_resource", placeholder="workflow_run")
    page = third.number_input("Page", min_value=1, value=1, step=1, key="audit_page")
    result = api.audit_logs(
        action=action.strip() or None,
        resource_type=resource.strip() or None,
        limit=PAGE_SIZE,
        offset=(int(page) - 1) * PAGE_SIZE,
    )
    st.caption(f"{result['total']} entries · newest first · read-only")
    st.dataframe(
        [
            {
                "when": short_time(row["created_at"]),
                "actor": row["actor_email"] or "-",
                "action": row["action"],
                "resource": f"{row['resource_type'] or '-'} {str(row['resource_id'] or '')[:8]}",
                "outcome": row["outcome"],
                "ip": row["ip_address"] or "",
            }
            for row in result["items"]
        ],
        hide_index=True,
        width="stretch",
    )
