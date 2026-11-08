"""Ranked review queue with snapshot-bound selection and explicit navigation."""

from functools import partial

import streamlit as st
from botocore.exceptions import BotoCoreError, ClientError

from src import config
from src.aws.athena import AthenaQueryError
from src.ui.pages.batch import (
    BRIEF_LABELS,
    DEMO_SOURCE,
    batch_source,
    get_backend,
    navigate,
)
from src.ui.services.errors import ServiceFailure, describe_failure
from src.ui.services.saved_results import SavedBrief

STALE_ALERT = "This alert is no longer in the loaded queue. Please select another alert."


def open_investigation(source: str) -> None:
    cached = st.session_state.get("ui_batch_snapshot")
    txn_id = st.session_state.get("ui_batch_queue_alert")
    snapshot = cached["snapshot"] if cached and cached["source"] == source else None
    entry = next((entry for entry in snapshot.entries if entry["txn_id"] == txn_id), None) if snapshot else None
    if entry is None:
        st.session_state["ui_batch_selection_notice"] = STALE_ALERT
        return
    st.session_state.pop("ui_batch_selection_notice", None)
    try:
        saved = get_backend(source).open(snapshot, txn_id)
    except (BotoCoreError, ClientError, OSError, ValueError, KeyError, TypeError) as error:
        failure = describe_failure(error, "Saved brief read")
        saved = SavedBrief("error", failure.message, failure=failure)
    if saved.status in {"error", "incompatible"}:
        entry["status"] = saved.status
        entry["Finding count"] = None
        entry["Finding codes"] = ""
    st.session_state["ui_batch_selection"] = {
        "source": source, "day": snapshot.day, "txn_id": txn_id, "entry": entry, "saved": saved,
    }
    st.session_state["ui_batch_member"] = txn_id
    st.session_state.pop("ui_batch_evidence", None)
    navigate("Investigation")


def select_queue_row(table_key: str, source: str, generation: int) -> None:
    """Ignore events from replaced tables; update selection before widgets render."""
    cached = st.session_state.get("ui_batch_snapshot")
    if not cached or cached["source"] != source or generation != st.session_state.get("ui_batch_generation"):
        return
    rows = st.session_state.get(table_key, {}).get("selection", {}).get("rows", [])
    entries = cached["snapshot"].entries
    if rows and isinstance(rows[0], int) and 0 <= rows[0] < len(entries):
        st.session_state["ui_batch_queue_alert"] = entries[rows[0]]["txn_id"]
        st.session_state.pop("ui_batch_selection_notice", None)


def render_queue() -> None:
    st.title("AML Alert Review")
    st.write("Review transactions prioritised by the risk model. Select an alert to inspect its money flow and supporting evidence.")
    source = batch_source()
    with st.expander("How to use this dashboard", expanded=False):
        st.write("1. Load ranked alerts.\n2. Select and investigate a transaction.\n3. Explore findings and supporting transactions.")
        st.caption("Daily rank preserves BQ1 priority. The risk score is a ranking signal, not a laundering probability.")
    cached = st.session_state.get("ui_batch_snapshot")
    previous = cached["snapshot"] if cached is not None and cached["source"] == source else None
    if st.session_state.get("ui_batch_controls_source") != source:
        st.session_state["ui_batch_controls_source"] = source
        st.session_state["ui_batch_top_k"] = previous.top_k if previous else 20
        st.session_state["ui_batch_day"] = previous.day if previous else config.AGENT_SCORED_DATES[0]
        st.session_state.pop("ui_batch_queue_alert", None)
    st.session_state.setdefault("ui_batch_day", previous.day if previous else config.AGENT_SCORED_DATES[0])
    st.session_state.setdefault("ui_batch_top_k", previous.top_k if previous else 20)
    with st.form("ui_batch_queue_form"):
        date_col, size_col, action_col = st.columns([2, 2, 2], vertical_alignment="bottom")
        date_col.selectbox("Analysis date", config.AGENT_SCORED_DATES, key="ui_batch_day",
                           index=config.AGENT_SCORED_DATES.index(previous.day) if previous else 0)
        size_col.number_input("Number of alerts to review", min_value=1, max_value=config.AGENT_QUEUE_MAX_K,
                              value=previous.top_k if previous else 20, step=1, key="ui_batch_top_k", help="Live starts with 20. Demo initially includes all available examples.")
        submitted = action_col.form_submit_button("Load / refresh queue", width="stretch")
    if submitted or (source == DEMO_SOURCE and (cached is None or cached["source"] != source)):
        try:
            snapshot = get_backend(source).load_queue(st.session_state["ui_batch_day"], int(st.session_state["ui_batch_top_k"]))
            cached = {"source": source, "snapshot": snapshot}
            st.session_state["ui_batch_snapshot"] = cached
            st.session_state["ui_batch_generation"] = st.session_state.get("ui_batch_generation", 0) + 1
            st.session_state.pop("ui_batch_queue_alert", None)
            st.session_state.pop("ui_batch_queue_error", None)
            st.session_state.pop("ui_batch_selection_notice", None)
        except (BotoCoreError, ClientError, AthenaQueryError, OSError, ValueError, KeyError, TypeError) as error:
            st.session_state["ui_batch_queue_error"] = (source, describe_failure(error, "Queue load"))
            st.session_state.pop("ui_batch_snapshot", None)
            st.session_state.pop("ui_batch_queue_alert", None)
            cached = None
    queue_error = st.session_state.get("ui_batch_queue_error")
    if queue_error is not None and queue_error[0] == source:
        st.error("Queue read failed. The selected data source remains active; no demo data was substituted.")
        st.error(queue_error[1].message)
        st.caption(f"Debug reference: {queue_error[1].reference}")
        return
    if cached is None or cached["source"] != source:
        st.info("Load a queue to begin browsing saved investigations.")
        return
    snapshot = cached["snapshot"]
    for failure in {failure.reference: failure for failure in snapshot.decisions.errors.values()
                    if isinstance(failure, ServiceFailure)}.values():
        st.warning(failure.message)
        st.caption(f"Debug reference: {failure.reference}")
    if notice := st.session_state.pop("ui_batch_selection_notice", None):
        st.warning(notice)
    st.caption(f"Loaded {snapshot.day} · {len(snapshot.entries)} alerts · Original BQ1 order")
    ids = [entry["txn_id"] for entry in snapshot.entries]
    if not ids:
        st.session_state.pop("ui_batch_queue_alert", None)
        st.info("No alerts are available for this date and review selection. Try another analysis date.")
        st.button("Open Selected Investigation", disabled=True)
        return
    if st.session_state.get("ui_batch_queue_alert") not in ids:
        st.session_state["ui_batch_queue_alert"] = ids[0]
    display = [{
        "Daily rank": entry["risk_rank_day"], "Transaction ID": entry["txn_id"],
        "Risk score": entry["risk_score"], "Transaction time (UTC)": entry["transaction_timestamp"],
        "Brief status": BRIEF_LABELS[entry["status"]],
        "Finding count": str(entry["Finding count"]) if entry["Finding count"] is not None else "—",
    } for entry in snapshot.entries]
    generation = st.session_state.get("ui_batch_generation", 0)
    table_key = f"ui_queue_table_{source}_{generation}_{ids.index(st.session_state['ui_batch_queue_alert'])}"
    st.caption("Select a row, then open its investigation. The selected-alert summary below confirms your choice.")
    st.dataframe(display, hide_index=True, width="stretch", height="auto", key=table_key,
                 on_select=partial(select_queue_row, table_key, source, generation), selection_mode="single-row",
                 selection_default={"selection": {"rows": [ids.index(st.session_state["ui_batch_queue_alert"])]}},
                 column_config={"Risk score": st.column_config.NumberColumn(format="%.4f"),
                                "Transaction ID": st.column_config.TextColumn(width=280),
                                "Brief status": st.column_config.TextColumn(width=190),
                                "Daily rank": st.column_config.NumberColumn(width=85),
                                "Finding count": st.column_config.TextColumn(width=95)})
    selected = snapshot.entries[ids.index(st.session_state["ui_batch_queue_alert"])]
    with st.container(border=True):
        summary, action = st.columns([3, 2], vertical_alignment="center")
        summary.text(f"Selected transaction: {selected['txn_id']}")
        summary.caption(f"Daily rank {selected['risk_rank_day']} · Risk score {selected['risk_score']:.4f} · "
                        f"{BRIEF_LABELS[selected['status']]}")
        action.button("Open Selected Investigation", type="primary", on_click=open_investigation,
                      args=(source,), width="stretch")
    with st.expander("Keyboard selection / alert details", expanded=False):
        st.selectbox("Selected alert", ids, key="ui_batch_queue_alert",
                     format_func=lambda txn: f"Rank {display[ids.index(txn)]['Daily rank']} · {txn}")
        entry = snapshot.entries[ids.index(st.session_state["ui_batch_queue_alert"])]
        st.text(f"Sender: {entry['source_account_key']} → Receiver: {entry['target_account_key']}")
        st.text(f"Case ID: {entry.get('case_id') or 'Not supplied'} · Role: {entry.get('role') or 'Not supplied'}")
        st.text(f"Finding codes: {entry.get('Finding codes') or 'None supplied'}")
        st.caption("Investigation available: a saved brief with findings. No supported findings: a saved brief with none. "
                   "No saved investigation: no published brief. Code-only fallback: fixed-rule output after an agent failure.")
