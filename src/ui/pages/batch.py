"""Shared source selection and session-scoped backend instances."""

import streamlit as st

from src.ui.pages.agent_demo import DEMO_NOTICE
from src.ui.services.demo import DemoBatchService
from src.ui.services.queue import LiveBatchService

DEMO_SOURCE = "Demo (offline)"
LIVE_SOURCE = "Live batch (read-only)"


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
        st.caption(
            "Synthetic queue ranks and scores are fixture values, not Athena results. "
            "txn_demounprocessed is an additional invented queue record. "
            "Top K limits sample rows; fixture ranks may tie or have gaps. "
            "Sample results illustrate different windows; they are not a historical batch replay."
        )
    else:
        st.info("LIVE BATCH — Read-only saved data. Reads begin only when you load a queue.")
    st.caption("Data is synthetic. Full analysis days end on 16 Sep 2022. Scores are ranking signals, not probabilities.")
    return source


def get_backend(source: str):
    backends = st.session_state.setdefault("ui_batch_backends", {})
    if source not in backends:
        backends[source] = DemoBatchService() if source == DEMO_SOURCE else LiveBatchService()
    return backends[source]


def navigate(page: str) -> None:
    st.session_state["ui_navigation_page"] = page
