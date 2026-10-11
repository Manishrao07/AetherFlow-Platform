"""Knowledge base: upload documents (chunked and indexed by the API) and search them."""

from __future__ import annotations

import streamlit as st

from ui import state
from ui.api_client import ApiClient, ApiError
from ui.format import plain, short_time

ACCEPTED = ["txt", "md", "markdown", "csv", "log"]
MAX_UPLOAD_BYTES = 1_000_000  # the API accepts up to ~1M characters


def render(api: ApiClient) -> None:
    st.header("Documents")
    caps = state.Capabilities.from_roles(api.roles)
    upload_tab, list_tab, search_tab = st.tabs(["Upload", "Library", "Search"])
    with upload_tab:
        _upload(api, caps)
    with list_tab:
        _library(api, caps)
    with search_tab:
        _search(api)


def _upload(api: ApiClient, caps: state.Capabilities) -> None:
    if not caps.can_create:
        st.info("Your role can read the knowledge base but not add to it.")
        return
    file = st.file_uploader("Text file", type=ACCEPTED, key="doc_file")
    pasted = st.text_area("…or paste text", height=140, key="doc_text")
    title = st.text_input(
        "Title", value=file.name.rsplit(".", 1)[0] if file else "", key="doc_title"
    )
    if not st.button("Ingest document", type="primary", key="doc_ingest"):
        return
    if file is not None:
        if file.size > MAX_UPLOAD_BYTES:
            st.error("That file is larger than 1 MB.")
            return
        try:
            content = file.getvalue().decode("utf-8")
        except UnicodeDecodeError:
            st.error("The file must be UTF-8 text.")
            return
    else:
        content = pasted
    if not title.strip() or not content.strip():
        st.warning("Give the document a title and some text.")
        return
    try:
        result = api.upload_document(title.strip(), content)
    except ApiError as exc:
        st.error(exc.message)
        return
    document = result["document"]
    if result["created"]:
        st.success(
            f"Ingested “{plain(document['title'])}” as "
            f"{document['chunk_count']} searchable chunk(s)."
        )
    else:
        st.info(f"Identical content is already in the library as “{plain(document['title'])}”.")


def _library(api: ApiClient, caps: state.Capabilities) -> None:
    documents = api.list_documents()
    if not documents:
        st.caption("The library is empty.")
        return
    for doc in documents:
        left, right = st.columns([6, 1])
        left.markdown(
            f"**{plain(doc['title'])}**  \n{doc['chunk_count']} chunk(s) · "
            f"{plain(doc['source'])} · added {short_time(doc['created_at'])}"
        )
        if right.button("Delete", key=f"del-{doc['id']}"):
            try:
                api.delete_document(doc["id"])
            except ApiError as exc:
                st.error(exc.message)
            else:
                st.rerun()


def _search(api: ApiClient) -> None:
    query = st.text_input("Ask the knowledge base", key="doc_query", placeholder="refund window")
    if not query.strip():
        return
    passages = api.search_documents(query.strip())
    if not passages:
        st.caption("No matching passages.")
    for passage in passages:
        with st.container(border=True):
            st.caption(f"{plain(passage['citation'])} · relevance {passage['score']}")
            st.markdown(plain(passage["content"]))
