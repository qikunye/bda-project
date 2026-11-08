"""Saved batch and explicit on-demand investigations, each with independent evidence."""

from functools import partial

import streamlit as st

from src.ui.components.agent_brief import render_agent_brief, render_selected_alert
from src.ui.components.agent_graph import render_agent_graph
from src.ui.components.evidence import render_evidence_buttons, render_selected_evidence
from src.ui.pages.batch import DEMO_SOURCE, batch_source, navigate
from src.ui.pages.on_demand import render_on_demand
from src.ui.services.saved_results import STATUS_LABELS


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
        selected_id = st.selectbox("Case member", ids, key=member_key,
                                   on_change=clear_evidence, args=(state_prefix,))
    else:
        selected_id = ids[0]
    # Streamlit removes an unrendered widget on view switches; retain the display choice separately.
    st.session_state[choice_key] = selected_id
    member = next(member for member in result["members"] if member["alert"]["txn_id"] == selected_id)
    render_selected_alert(member["alert"], result["window_h"])
    render_selected_evidence(result, selected_id, state_prefix=state_prefix)
    cited_ids = {evidence_id for finding in result["findings"] for evidence_id in finding["evidence_ids"]}
    render_agent_graph(member, cited_ids, st.session_state.get(f"{state_prefix}_evidence"))
    render_agent_brief(result, selected_txn_id=selected_id, show_alert=False,
                       evidence_renderer=partial(render_evidence_buttons, result, state_prefix=state_prefix))


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
    st.caption(f"Analysis date: {selection['day']} · {STATUS_LABELS[saved.status]}")
    if saved.result is None:
        if saved.status in {"error", "incompatible"}:
            st.error(saved.detail)
            if saved.failure:
                st.caption(f"Debug reference: {saved.failure.reference}")
        else:
            st.info("No batch result is stored for this alert. On-demand execution requires an explicit submission.")
        entry = selection["entry"]
        st.json({key: entry[key] for key in (
            "transaction_id", "transaction_timestamp", "source_account_key", "target_account_key",
            "risk_score", "risk_rank_day",
        )})
    attempt, request_id = render_on_demand(selection, source)
    views = (["Saved Batch Brief"] if saved.result is not None else []) + (
        ["On-Demand Brief"] if attempt is not None and attempt.result is not None else []
    )
    if not views:
        return
    st.divider()
    view_key = f"ui_on_demand_view_{request_id}"
    if st.session_state.get(view_key) not in views:
        st.session_state[view_key] = views[0]
    view = st.radio("Evidence view", views, key=view_key)
    if len(views) > 1:
        st.caption("The views have independent case membership, windows and data cutoffs. "
                   "Their findings should not be compared as if they used identical evidence.")
    st.subheader(view)
    if view == "Saved Batch Brief":
        result = saved.result
        st.caption(f"Saved run: {saved.run_id} · Created: {saved.created_at}")
        st.text(f"Representative alert: {result['txn_id']}")
        st.text(f"Case ID: {result['case_id'] or 'Single-alert brief'} · {len(result['members'])} member(s)")
        st.caption(f"Original saved investigation window: ±{result['window_h']} hours")
        if source != DEMO_SOURCE:
            st.caption("Saved daily batch: observations are bounded by the end of the analysis day and dataset horizon.")
        if saved.scenario == "code_only":
            st.info("Code-only demonstration — fixed rules built this brief; this is not a fallback.")
        render_result(result, selection["txn_id"])
    else:
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
