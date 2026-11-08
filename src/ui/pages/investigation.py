"""Open saved cases without re-executing tools or the LLM."""

from functools import partial

import streamlit as st

from src.ui.components.agent_brief import render_agent_brief, render_selected_alert
from src.ui.components.agent_graph import render_agent_graph
from src.ui.components.evidence import render_evidence_buttons, render_selected_evidence
from src.ui.pages.batch import batch_source, navigate
from src.ui.services.saved_results import STATUS_LABELS


def clear_evidence() -> None:
    st.session_state.pop("ui_batch_evidence", None)


def render_investigation() -> None:
    st.title("Investigation")
    source = batch_source()
    st.button("Back to Alert Queue", on_click=navigate, args=("Alert Queue",))
    selection = st.session_state.get("ui_batch_selection")
    if selection is None or selection["source"] != source:
        st.info("Select an alert from this source's queue to open its saved investigation.")
        return
    saved = selection["saved"]
    st.text(f"Opened queue alert: {selection['txn_id']}")
    st.caption(f"Analysis date: {selection['day']} · {STATUS_LABELS[saved.status]}")
    if saved.result is None:
        if saved.status in {"error", "incompatible"}:
            st.error(saved.detail)
        else:
            st.info("No batch result is stored for this alert. No investigation has been started.")
        entry = selection["entry"]
        st.json({key: entry[key] for key in (
            "transaction_id", "transaction_timestamp", "source_account_key", "target_account_key",
            "risk_score", "risk_rank_day",
        )})
        return
    result = saved.result
    st.caption(f"Saved run: {saved.run_id} · Created: {saved.created_at}")
    st.text(f"Representative alert: {result['txn_id']}")
    st.text(f"Case ID: {result['case_id'] or 'Single-alert brief'} · {len(result['members'])} member(s)")
    if source.startswith("Live"):
        st.caption("Saved daily batch: observations are bounded by the end of the analysis day and dataset horizon.")
    if saved.scenario == "code_only":
        st.info("Code-only demonstration — fixed rules built this brief; this is not a fallback.")
    ids = [member["alert"]["txn_id"] for member in result["members"]]
    if st.session_state.get("ui_batch_member") not in ids:
        st.session_state["ui_batch_member"] = selection["txn_id"] if selection["txn_id"] in ids else ids[0]
    if len(ids) > 1:
        selected_id = st.selectbox("Case member", ids, key="ui_batch_member", on_change=clear_evidence)
    else:
        selected_id = ids[0]
    member = next(member for member in result["members"] if member["alert"]["txn_id"] == selected_id)
    render_selected_alert(member["alert"], result["window_h"])
    render_selected_evidence(result, selected_id)
    cited_ids = {evidence_id for finding in result["findings"] for evidence_id in finding["evidence_ids"]}
    render_agent_graph(member, cited_ids, st.session_state.get("ui_batch_evidence"))
    render_agent_brief(result, selected_txn_id=selected_id, show_alert=False,
                       evidence_renderer=partial(render_evidence_buttons, result))
