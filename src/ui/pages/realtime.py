"""Manual, bounded monitoring snapshots; no polling or pipeline execution."""

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

import streamlit as st

from src.ui.components.notices import render_project_notices
from src.ui.components.realtime import (
    explanation_status,
    monitoring_rows,
    render_realtime_record,
    threshold_status,
)
from src.ui.services.errors import ServiceFailure, describe_failure
from src.ui.services.realtime import (
    MAX_LOOKUPS,
    RealtimeRecord,
    RealtimeService,
    validate_ids,
)
from src.ui.services.realtime_demo import DEMO_GROUPS, DemoRealtimeService
from src.ui.services.realtime_feed import FeedResult, RealtimeFeedService

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


def render_manual_lookup() -> None:
    sources = [DEMO_SOURCE, LIVE_SOURCE]
    source = st.radio("Manual lookup source", sources, key="ui_rt_source",
                      index=sources.index(st.session_state.get("ui_rt_source_choice", DEMO_SOURCE)))
    st.session_state["ui_rt_source_choice"] = source
    st.caption("Known-ID lookup: at most 20 IDs. Batch IDs need not exist in the real-time pipeline.")
    if source == DEMO_SOURCE:
        st.caption("DEMO DATA — invented offline records; no AWS execution.")
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
            if submitted:
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


INDEX_SOURCE = "Live Recent Alerts (indexed DynamoDB)"
S3_SOURCE = "Saved Replay Results"


def filter_records(records, risk="All scores", minimum=0.0, status="All statuses"):
    def matches(record):
        if status != "All statuses" and explanation_status(record) != status:
            return False
        if risk == "Minimum score":
            return record.alert is not None and record.alert.score is not None and record.alert.score >= minimum
        if risk in {"Meets scoring threshold", "Below scoring threshold"}:
            return threshold_status(record) == risk
        return True
    return [record for record in records if matches(record)]


FEED_CONTROL_KEYS = (
    "ui_rt_feed_source", "ui_rt_day_index", "ui_rt_day_replay", "ui_rt_feed_limit",
    "ui_rt_feed_risk", "ui_rt_feed_minimum", "ui_rt_feed_status", "ui_rt_feed_selected",
)


def render_realtime() -> None:
    # Streamlit removes widget keys when navigating away. Keep only local control
    # values separately so returning to a loaded snapshot never needs a new read.
    for key, value in st.session_state.get("ui_rt_feed_controls", {}).items():
        if key not in st.session_state:
            st.session_state[key] = value
    if st.session_state.get("ui_rt_feed_source") == "Available Completed Explanations (S3 discovery)":
        st.session_state["ui_rt_feed_source"] = S3_SOURCE
    st.title("Real-Time Monitoring")
    st.caption("Discover scored transactions, select one, and review its Qwen explanation. Load or refresh explicitly; no polling or pipeline health monitoring.")
    render_project_notices()
    index_name = os.environ.get("REALTIME_ALERTS_INDEX", "").strip()
    sources = [DEMO_SOURCE] + ([INDEX_SOURCE] if index_name else []) + [S3_SOURCE]
    if st.session_state.get("ui_rt_feed_source") not in sources:
        st.session_state["ui_rt_feed_source"] = DEMO_SOURCE
    source = st.sidebar.radio("Data Source", sources, key="ui_rt_feed_source")
    if source == DEMO_SOURCE:
        st.warning("DEMO DATA — invented samples of offline records and explanations; no AWS or agent execution.")
    elif source == INDEX_SOURCE:
        st.info("LIVE INDEX — newest processing timestamps first, within one UTC processing day. "
                "Processing time differs from the historical 2022 transaction time. The index is eventually consistent.")
    else:
        st.info("These transactions were replayed through our real-time pipeline using historical data. "
                "Select a date to view their risk scores and Qwen explanations.")
    technical_details = st.expander("Technical Details", expanded=False)
    with technical_details:
        if not index_name:
            st.caption("Indexed recent alerts are not configured. Saved Replay Results and manual lookup are available.")
        st.caption("Saved Replay Results discovers S3 explanation-object keys under rt/briefs/dt=YYYY-MM-DD/. "
                   "This is not a complete list of scored transactions and is not ordered by actual processing time. "
                   "Explanation coverage may be incomplete. Indexed recent alerts require a separate chronological GSI.")
        st.text("Kinesis → is459-rt-scorer → is459_rt_alerts → is459-rt-explainer → is459_rt_briefs → Streamlit")
        st.caption("Scoring Lambda supplies BQ1 risk scores; Qwen2.5 + LoRA supplies explanations and next checks. "
                   "This page reads stored outputs only. No stream, Lambda or investigation agent is started.")
    st.subheader("Replay Results" if source == S3_SOURCE else "Recent Alerts")
    left, right = st.columns([2, 1])
    with left:
        day = st.date_input("UTC processing date" if source == INDEX_SOURCE else "Transaction Date",
                            value=datetime.now(timezone.utc).date() if source == INDEX_SOURCE else date(2022, 9, 16),
                            disabled=source == DEMO_SOURCE,
                            key=f"ui_rt_day_{'index' if source == INDEX_SOURCE else 'replay'}")
    with right:
        limit = st.number_input("Records to load", min_value=1, max_value=50, value=20, step=1, key="ui_rt_feed_limit")
    request = (source, day, limit, index_name if source == INDEX_SOURCE else "")
    if st.session_state.get("ui_rt_feed_request") != request:
        st.session_state["ui_rt_feed_request"] = request
        st.session_state.pop("ui_rt_feed_snapshot", None)
        st.session_state.pop("ui_rt_feed_selected", None)
    load_col, refresh_col = st.columns(2)
    load_label = "Load Results" if source == S3_SOURCE else "Load Recent Alerts"
    submitted = load_col.button(load_label, key="ui_rt_feed_load")
    refresh = refresh_col.button("Refresh", key="ui_rt_feed_refresh",
                                 disabled="ui_rt_feed_snapshot" not in st.session_state)
    if submitted or refresh:
        result, failure = None, None
        try:
            if source == DEMO_SOURCE:
                result = FeedResult(DemoRealtimeService().lookup_many(DEMO_GROUPS["All sample states"])[:limit])
            else:
                services = st.session_state.setdefault("ui_rt_feed_services", {})
                service_key = (source, index_name)
                if service_key not in services:
                    services[service_key] = RealtimeFeedService(index_name=index_name)
                service = services[service_key]
                result = service.recent(day, limit) if source == INDEX_SOURCE else service.completed(day, limit)
        except Exception as error:  # noqa: BLE001 -- safe presentation boundary; never substitute demo data.
            failure = describe_failure(error, "Real-time discovery")
        st.session_state["ui_rt_feed_snapshot"] = (
            result, failure, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
    snapshot = st.session_state.get("ui_rt_feed_snapshot")
    if snapshot is None:
        st.info(f"Choose a source and date, then click {load_label}.")
    else:
        result, failure, retrieved_at = snapshot
        st.caption(f"{source} · {day.isoformat()} · Last retrieval attempt: {retrieved_at}")
        if failure:
            st.error(failure.message)
            st.caption(f"Debug reference: {failure.reference}. No demo records were substituted.")
        else:
            if result.limited:
                st.warning("Showing a limited set of saved results." if source == S3_SOURCE else
                           "Discovery is limited / incomplete. More records may exist.")
                with technical_details:
                    st.caption("The record or three-page listing bound was reached; the listing may be incomplete.")
            if result.skipped:
                with technical_details:
                    st.caption(f"Skipped {result.skipped} keys that did not match the explanation filename contract.")
            st.caption("BQ1 risk scores are ranking signals, not calibrated fraud probabilities. "
                       "Filters apply only to loaded records; missing thresholds are not classified as suspicious.")
            risk = st.selectbox("Risk-score filter", ["All scores", "Minimum score", "Meets scoring threshold", "Below scoring threshold"],
                                key="ui_rt_feed_risk")
            minimum = st.number_input("Minimum risk score", value=0.0, key="ui_rt_feed_minimum") if risk == "Minimum score" else 0.0
            status = st.selectbox("Explanation-status filter", ["All statuses", "Explanation available", "Explanation pending",
                                  "Fallback / needs review", "Data unavailable / limited explanation", "Data unavailable", "AWS read failure"],
                                  key="ui_rt_feed_status")
            records = filter_records(result.records, risk, minimum, status)
            if not result.records:
                st.info("No saved results found for this date. Try 16 September 2022, which contains our verified demo examples."
                        if source == S3_SOURCE else
                        "No records discovered for this date within the bounded listing. Try another date or manual lookup.")
            elif not records:
                st.info("No loaded records match these filters.")
            else:
                st.dataframe(monitoring_rows(records), hide_index=True, width="stretch")
                ids = [record.txn for record in records]
                if st.session_state.get("ui_rt_feed_selected") not in ids:
                    st.session_state["ui_rt_feed_selected"] = ids[0]
                selected = st.selectbox("Select transaction", ids, key="ui_rt_feed_selected")
                render_realtime_record(next(record for record in records if record.txn == selected),
                                       heading="Selected Alert Details")
    st.caption("BQ1 screening and Qwen pipeline interpretations only. Reviewing or filtering these records does not initiate BQ2 / Claude investigation.")
    with st.expander("Advanced: Manual Transaction Lookup", expanded=False):
        render_manual_lookup()
    st.session_state["ui_rt_feed_controls"] = {
        key: st.session_state[key] for key in FEED_CONTROL_KEYS if key in st.session_state
    }
