"""Shared source selection and session-scoped backend instances."""

import streamlit as st

from src.ui.components.notices import render_project_notices
from src.ui.pages.agent_demo import DEMO_NOTICE
from src.ui.services.demo import DemoBatchService
from src.ui.services.queue import LiveBatchService

DEMO_SOURCE = "Demo (offline)"
LIVE_SOURCE = "Live batch (read-only)"

BRIEF_LABELS = {
    "saved": "Investigation available",
    "missing": "No saved investigation",
    "empty": "No supported findings",
    "fallback": "Code-only fallback",
    "error": "Unable to load investigation",
    "incompatible": "Incompatible saved investigation",
}


def batch_source() -> str:
    sources = [DEMO_SOURCE, LIVE_SOURCE]
    source = st.sidebar.radio(
        "Data source", sources, key="ui_batch_source",
        index=sources.index(st.session_state.get("ui_batch_source_choice", DEMO_SOURCE)),
    )
    # Keep the source through pages where this widget is intentionally absent.
    st.session_state["ui_batch_source_choice"] = source
    if source == DEMO_SOURCE:
        st.warning(DEMO_NOTICE)
    else:
        st.info("LIVE BATCH — Stored results from the synthetic IBM AML dataset. Load a queue to read AWS records.")
    with st.sidebar.expander("About this data source", expanded=False):
        st.caption("Synthetic queue ranks and scores are fixture values, not Athena results. "
                   "Demo records illustrate result shapes, not a historical batch replay."
                   if source == DEMO_SOURCE else
                   "Queue reads use Athena; saved investigations use DynamoDB and S3. "
                   "Opening a saved investigation does not run the agent.")
    render_project_notices()
    return source


def get_backend(source: str):
    backends = st.session_state.setdefault("ui_batch_backends", {})
    if source not in backends:
        backends[source] = DemoBatchService() if source == DEMO_SOURCE else LiveBatchService()
    return backends[source]


def navigate(page: str) -> None:
    st.session_state["ui_navigation_page"] = page
