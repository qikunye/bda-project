"""Saved batch and explicit on-demand investigations, each with independent evidence."""

from functools import partial

import streamlit as st

from src.ui.components.agent_brief import (
    render_agent_brief,
    render_findings,
    render_gaps_and_checks,
    render_money_flow,
    render_observations,
    render_provenance,
    render_selected_alert,
)
from src.ui.components.agent_graph import render_agent_graph
from src.ui.components.evidence import render_evidence_buttons, render_selected_evidence
from src.ui.components.formatting import format_number
from src.ui.pages.batch import BRIEF_LABELS, DEMO_SOURCE, batch_source, navigate
from src.ui.pages.on_demand import render_on_demand


def clear_evidence(state_prefix: str = "ui_batch") -> None:
    st.session_state.pop(f"{state_prefix}_evidence", None)


def render_result(result: dict, default_txn_id: str, *, state_prefix: str = "ui_batch") -> None:
    if result["status"] == "not_found":
        render_agent_brief(result)
        return
    ids = [member["alert"]["txn_id"] for member in result["members"]]
    member_key = f"{state_prefix}_member"
    choice_key = f"{state_prefix}_member_choice"
    if st.session_state.get(member_key) not in ids:
        previous = st.session_state.get(choice_key, default_txn_id)
        st.session_state[member_key] = previous if previous in ids else ids[0]
    if len(ids) > 1:
        st.caption("Several flagged transactions are investigated together. Switch members to explore each payment; "
                   "some findings apply to the whole case.")
        selected_id = st.selectbox("Case member", ids, key=member_key,
                                   on_change=clear_evidence, args=(state_prefix,))
    else:
        selected_id = ids[0]
    # Streamlit removes an unrendered widget on view switches; retain the display choice separately.
    st.session_state[choice_key] = selected_id
    member = next(member for member in result["members"] if member["alert"]["txn_id"] == selected_id)
    render_selected_alert(member["alert"], result["window_h"])
    cited_ids = {evidence_id for finding in result["findings"] for evidence_id in finding["evidence_ids"]}
    st.subheader("Investigation summary")
    if result["summary"]:
        st.write(result["summary"])
    if not result["findings"]:
        st.info("The available evidence contains no supported findings. This does not establish that the transaction is safe.")
    render_findings(result, partial(render_evidence_buttons, result, state_prefix=state_prefix))
    graph_col, detail_col = st.columns([3, 2], gap="large")
    with graph_col:
        render_agent_graph(member, cited_ids, st.session_state.get(f"{state_prefix}_evidence"))
    with detail_col:
        render_selected_evidence(result, selected_id, state_prefix=state_prefix)
    st.subheader("Additional analysis")
    with st.expander("Money-flow metrics", expanded=False):
        flow = next((flow for flow in result["money_flow"] if flow["txn_id"] == selected_id), None)
        render_money_flow(flow)
    render_gaps_and_checks(result)
    st.subheader("Technical details")
    with st.expander("Agent observations · unverified interpretation", expanded=False):
        render_observations(result)
    render_provenance(result)


def render_investigation() -> None:
    st.title("Investigation")
    source = batch_source()
    st.button("Back to Alert Queue", on_click=navigate, args=("Alert Queue",))
    selection = st.session_state.get("ui_batch_selection")
    if selection is None or selection["source"] != source:
        st.info("Select an alert from this source's queue to open its investigation.")
        return
    saved = selection["saved"]
    st.text(f"Opened queue alert: {selection['txn_id']}")
    st.caption(f"Analysis date: {selection['day']} · {BRIEF_LABELS[saved.status]}")
    if saved.result is None:
        if saved.status in {"error", "incompatible"}:
            st.error(saved.detail)
            if saved.failure:
                st.caption(f"Debug reference: {saved.failure.reference}")
        entry = selection["entry"]
        st.text(f"{entry['source_account_key']} → {entry['target_account_key']}")
        st.caption(f"Daily rank {entry['risk_rank_day']} · Risk score {format_number(entry.get('risk_score'))} · "
                   f"Time: {entry['transaction_timestamp']} UTC")
    result_area = st.container()
    with st.expander("Advanced: Run a new AI investigation", expanded=False):
        st.write("Saved investigation: a previous batch result. Run new investigation: starts a separate agent "
                 "execution and may incur AWS costs. Completed results remain in this session only.")
        attempt, request_id = render_on_demand(selection, source)
    with result_area:
        views = (["Previous Investigation"] if saved.result is not None else []) + (
            ["New Investigation"] if attempt is not None and attempt.result is not None else []
        )
        if not views:
            st.info("No investigation report is available for this alert. You can explicitly request a new investigation "
                    "using the advanced controls below.")
            return
        st.divider()
        view_key = f"ui_on_demand_view_{request_id}"
        # Migrate the earlier labels, including the execution control's retained
        # default, without changing request execution or evidence state.
        previous_view = st.session_state.get(view_key)
        migrated_view = {"Saved Batch Brief": "Previous Investigation", "On-Demand Brief": "New Investigation"}.get(
            previous_view, previous_view,
        )
        if migrated_view not in views:
            migrated_view = views[0]
        if st.session_state.get(view_key) != migrated_view:
            st.session_state[view_key] = migrated_view
        if len(views) == 2:
            view = st.radio("Investigation report", views, key=view_key, horizontal=True)
            st.caption("The previous report was generated during batch processing; the new report was explicitly "
                       "requested by the analyst. Their case membership, observation windows and evidence can differ."
                       if source != DEMO_SOURCE else
                       "These synthetic examples illustrate a previous batch investigation and an analyst-requested "
                       "new investigation. Their case membership, observation windows and evidence can differ.")
        else:
            view = views[0]
        st.subheader("Investigation Report" if view == "Previous Investigation" else "New Investigation Report")
        if view == "Previous Investigation":
            st.caption("Previously generated investigation, retrieved from AWS. Opening this report does not rerun the AI agent."
                       if source != DEMO_SOURCE else
                       "Previously generated example investigation, loaded from synthetic fixtures. Opening this report does not rerun the AI agent.")
            result = saved.result
            st.caption(f"Saved case · {len(result['members'])} member(s) · Window: ±{result['window_h']} hours")
            with st.expander("Saved investigation details", expanded=False):
                st.caption(f"Saved run: {saved.run_id} · Created: {saved.created_at}")
                st.caption(f"Original saved investigation window: ±{result['window_h']} hours")
                st.text(f"Representative alert: {result['txn_id']}")
                st.text(f"Case ID: {result['case_id'] or 'Single-alert brief'} · {len(result['members'])} member(s)")
                if source != DEMO_SOURCE:
                    st.caption("Saved daily batch: observations are bounded by the end of the analysis day and dataset horizon.")
            if saved.scenario == "code_only":
                st.info("Code-only demonstration — fixed rules built this brief; this is not a fallback.")
            render_result(result, selection["txn_id"])
        else:
            st.caption("Explicitly requested by the analyst. This report is retained in the current session; viewing it does not rerun the AI agent.")
            context = (request_id, attempt.generation)
            if st.session_state.get("ui_on_demand_display_context") != context:
                clear_evidence("ui_on_demand_display")
                st.session_state.pop("ui_on_demand_display_member", None)
                st.session_state["ui_on_demand_display_context"] = context
            if source == DEMO_SOURCE:
                st.info("SIMULATED ON-DEMAND RESULT — An existing single-alert fixture; no real investigation ran.")
            st.caption(f"Unsaved single-alert result · Completed: {attempt.completed_at} · "
                       f"Requested window: ±{attempt.result['window_h']} hours")
            st.caption("On-demand evidence can include later transactions up to the dataset horizon. "
                       "It has no batch end-of-day cutoff and does not change the saved case.")
            if saved.scenario == "code_only" and source == DEMO_SOURCE:
                st.info("Code-only demonstration — fixed rules built this brief; this is not a fallback.")
            render_result(attempt.result, selection["txn_id"], state_prefix="ui_on_demand_display")
