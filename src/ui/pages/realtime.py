"""Manual, bounded monitoring snapshots; no polling or pipeline execution."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import streamlit as st

from src.ui.components.notices import render_project_notices
from src.ui.components.realtime import monitoring_rows, render_realtime_record
from src.ui.services.errors import ServiceFailure, describe_failure
from src.ui.services.realtime import (
    MAX_LOOKUPS,
    RealtimeRecord,
    RealtimeService,
    validate_ids,
)
from src.ui.services.realtime_demo import DEMO_GROUPS, DemoRealtimeService

DEMO_SOURCE = "Demo (offline)"
LIVE_SOURCE = "Live DynamoDB (read-only)"


@dataclass
class MonitoringSnapshot:
    source: str
    txn_ids: list[str]
    records: list[RealtimeRecord]
    retrieved_at: str | None
    label: str
    failure: ServiceFailure | None = None


def _service(source: str):
    services = st.session_state.setdefault("ui_rt_services", {})
    if source not in services:
        services[source] = DemoRealtimeService() if source == DEMO_SOURCE else RealtimeService()
    return services[source]


def render_realtime() -> None:
    st.title("Real-Time Alert Lookup")
    st.caption("View a scored alert, then read its explanation when available. Load or refresh explicitly; this page does not poll.")
    sources = [DEMO_SOURCE, LIVE_SOURCE]
    source = st.sidebar.radio("Real-time data source", sources, key="ui_rt_source",
                              index=sources.index(st.session_state.get("ui_rt_source_choice", DEMO_SOURCE)))
    st.session_state["ui_rt_source_choice"] = source
    if source == DEMO_SOURCE:
        st.warning("DEMO DATA — All real-time records, timestamps, groups and explanations here are invented samples. "
                   "No Kinesis, Lambda, DynamoDB or agent execution occurred.")
    else:
        st.info("LIVE DYNAMODB — Synthetic IBM AML pipeline records, known transaction IDs only. Reads start on Load or Refresh, never on source selection.")
    st.caption("A scored alert can appear before its explanation. These explanations come from the separate "
               "Lambda pipeline, not the investigation agent. Known-ID lookup only; this is not a live feed.")
    render_project_notices()
    with st.expander("Pipeline and integration scope", expanded=False):
        st.text("Kinesis → is459-rt-scorer → is459_rt_alerts → is459-rt-explainer → is459_rt_briefs → Streamlit")
        st.write("This lookup reads persisted records only. It does not report pipeline health or start/stop sessions. "
                 "Automatic recent-alert listing is unavailable: the tables have no chronological index, "
                 "and the exported session helper discovers alerts by scanning, which this UI does not use. "
                 "No table scan is used.")

    if st.session_state.get("ui_rt_input_source") != source:
        st.session_state["ui_rt_input_source"] = source
        st.session_state.pop("ui_rt_input_error", None)
    snapshot = st.session_state.get("ui_rt_snapshot")
    if snapshot is not None and snapshot.source != source:
        st.session_state.pop("ui_rt_snapshot", None)
        snapshot = None
    with st.form("ui_rt_lookup_form"):
        group = st.selectbox("Demo scenario / sample session", list(DEMO_GROUPS), key="ui_rt_demo_group") if source == DEMO_SOURCE else None
        ids_text = st.text_area(
            "Known transaction IDs" + (" (optional override)" if source == DEMO_SOURCE else ""),
            key="ui_rt_demo_ids" if source == DEMO_SOURCE else "ui_rt_live_ids",
            help=f"One ID per line or separated by commas. At most {MAX_LOOKUPS} IDs; input order is retained.",
        )
        submitted = st.form_submit_button("Load known alerts")
    refresh = st.button("Refresh loaded alerts", disabled=snapshot is None or snapshot.failure is not None)
    if submitted or refresh:
        # Input errors are local guidance, not AWS failures. Keep the loaded
        # snapshot and its timestamp unchanged until a lookup actually occurs.
        try:
            if refresh:
                txn_ids, label = validate_ids(snapshot.txn_ids), snapshot.label
            else:
                raw_ids = [item.strip() for item in re.split(r"[,\n]+", ids_text) if item.strip()] if ids_text.strip() else DEMO_GROUPS.get(group, [])
                txn_ids = validate_ids(raw_ids)
                label = group if source == DEMO_SOURCE and not ids_text.strip() else "Explicit known-ID lookup"
        except ValueError as error:
            st.session_state["ui_rt_input_error"] = str(error)
        else:
            st.session_state.pop("ui_rt_input_error", None)
            try:
                records = _service(source).lookup_many(txn_ids)
                snapshot = MonitoringSnapshot(source, txn_ids, records, datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"), label)
            except Exception as error:  # noqa: BLE001 -- service boundary; no raw provider errors or fallback.
                snapshot = MonitoringSnapshot(source, [], [], None, "Lookup failed",
                                              describe_failure(error, "Real-time lookup"))
            st.session_state["ui_rt_snapshot"] = snapshot
            st.session_state.pop("ui_rt_selected_txn", None)
    if message := st.session_state.get("ui_rt_input_error"):
        st.error(message)
    if snapshot is None:
        st.info("Enter one or more transaction IDs processed by the real-time pipeline. The dashboard retrieves "
                "stored alerts and Lambda explanations from DynamoDB."
                if source == LIVE_SOURCE else
                "Load a sample group to explore stored alerts and Lambda explanation examples without AWS.")
        st.caption("IDs from the batch alert queue are not necessarily present in the separate real-time pipeline.")
        return
    if snapshot.retrieved_at is not None:
        st.caption(f"{snapshot.label} · Last lookup completed: {snapshot.retrieved_at} "
                   "(retrieval time, not event freshness or processing latency)")
    if snapshot.failure is not None:
        st.error(snapshot.failure.message)
        st.caption(f"Debug reference: {snapshot.failure.reference}. No demo records were substituted.")
        st.info(f"Use Load known alerts with 1–{MAX_LOOKUPS} valid IDs to retry explicitly.")
        return
    if not snapshot.records:
        st.info("No records were returned for the requested ID set.")
        return
    st.caption("Records are in requested-ID order; this is not a chronological recent-alert feed.")
    st.dataframe(monitoring_rows(snapshot.records), hide_index=True, width="stretch")
    ids = [record.txn for record in snapshot.records]
    if st.session_state.get("ui_rt_selected_txn") not in ids:
        previous = st.session_state.get("ui_rt_selected_choice")
        st.session_state["ui_rt_selected_txn"] = previous if previous in ids else ids[0]
    selected = st.selectbox("Select real-time transaction", ids, key="ui_rt_selected_txn")
    st.session_state["ui_rt_selected_choice"] = selected
    render_realtime_record(next(record for record in snapshot.records if record.txn == selected))
