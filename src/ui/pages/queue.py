"""Daily BQ1 queue with saved-brief annotations and explicit investigation navigation."""

import streamlit as st
from botocore.exceptions import BotoCoreError, ClientError

from src import config
from src.ui.pages.batch import DEMO_SOURCE, batch_source, get_backend, navigate
from src.ui.services.saved_results import STATUS_LABELS, SavedBrief


def open_investigation(source: str) -> None:
    snapshot = st.session_state["ui_batch_snapshot"]["snapshot"]
    txn_id = st.session_state["ui_batch_queue_alert"]
    entry = next(entry for entry in snapshot.entries if entry["txn_id"] == txn_id)
    try:
        saved = get_backend(source).open(snapshot, txn_id)
    except (BotoCoreError, ClientError, OSError, ValueError, KeyError, TypeError):
        saved = SavedBrief("error", "Saved brief could not be loaded. Reload the queue to retry.")
    if saved.status in {"error", "incompatible"}:
        entry["status"] = saved.status
        entry["Brief availability"] = STATUS_LABELS[saved.status]
        entry["Finding count"] = None
        entry["Finding codes"] = ""
    st.session_state["ui_batch_selection"] = {
        "source": source, "day": snapshot.day, "txn_id": txn_id, "entry": entry, "saved": saved,
    }
    st.session_state["ui_batch_member"] = txn_id
    st.session_state.pop("ui_batch_evidence", None)
    navigate("Investigation")


def render_queue() -> None:
    st.title("AML Investigation Dashboard")
    st.subheader("Alert Queue")
    source = batch_source()
    cached = st.session_state.get("ui_batch_snapshot")
    previous = cached["snapshot"] if cached is not None and cached["source"] == source else None
    with st.form("ui_batch_queue_form"):
        day = st.selectbox("Analysis date", config.AGENT_SCORED_DATES, key="ui_batch_day",
                           index=config.AGENT_SCORED_DATES.index(previous.day) if previous else 0)
        top_k = st.number_input("Review top K", min_value=1, max_value=config.AGENT_QUEUE_MAX_K,
                                value=previous.top_k if previous else config.AGENT_TOP_K,
                                step=1, key="ui_batch_top_k")
        submitted = st.form_submit_button("Load / refresh queue")
    if submitted or (source == DEMO_SOURCE and (cached is None or cached["source"] != source)):
        try:
            snapshot = get_backend(source).load_queue(day, int(top_k))
            cached = {"source": source, "snapshot": snapshot}
            st.session_state["ui_batch_snapshot"] = cached
            st.session_state.pop("ui_batch_queue_alert", None)
            st.session_state.pop("ui_batch_queue_error", None)
        except (BotoCoreError, ClientError, OSError, ValueError, KeyError, TypeError):
            st.session_state["ui_batch_queue_error"] = source
            st.session_state.pop("ui_batch_snapshot", None)
            cached = None
    if st.session_state.get("ui_batch_queue_error") == source:
        st.error("Queue read failed. The selected data source remains active; no demo data was substituted.")
        return
    if cached is None or cached["source"] != source:
        st.info("Load a queue to begin browsing saved batch results.")
        return
    snapshot = cached["snapshot"]
    st.caption(f"Loaded {snapshot.day} · Top {snapshot.top_k} · {len(snapshot.entries)} alerts · Original BQ1 order")
    if not snapshot.entries:
        st.info("No alerts are available for this date and review selection.")
        return
    display = [{
        "BQ1 daily rank": entry["risk_rank_day"], "Transaction ID": entry["txn_id"],
        "Timestamp (UTC)": entry["transaction_timestamp"], "Sender": entry["source_account_key"],
        "Receiver": entry["target_account_key"], "BQ1 risk score": entry["risk_score"],
        "Brief availability": entry["Brief availability"], "Finding count": entry["Finding count"],
        "Finding codes": entry["Finding codes"], "Case ID": entry["case_id"], "Role": entry["role"],
    } for entry in snapshot.entries]
    st.dataframe(display, hide_index=True, width="stretch")
    st.caption("Availability is based on decision metadata; full case and trace compatibility is checked when opened.")
    st.selectbox("Select alert", [entry["txn_id"] for entry in snapshot.entries], key="ui_batch_queue_alert")
    st.button("Open Investigation", type="primary", on_click=open_investigation, args=(source,))
