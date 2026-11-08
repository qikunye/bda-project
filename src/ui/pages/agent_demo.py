"""Offline evidence-brief demonstration using the agent's existing fixtures."""

import streamlit as st

from src.agent.sample_results import sample_investigations
from src.ui.components.agent_brief import render_agent_brief

SCENARIO_LABELS = {
    "pass_through": "Money passing through",
    "fan_out_case": "Grouped case: Fan-out and Fan-in",
    "money_returns": "Money returning to its source",
    "quiet_truncated": "No findings: Truncated observation window",
    "no_graph": "No graph: Self-transfer",
    "code_only": "Code-only brief",
    "repaired": "Brief accepted after repair",
    "fallback": "Code-only fallback after LLM failure",
    "not_found": "Transaction not found",
}

DEMO_NOTICE = (
    "DEMO DATA — All transactions, account IDs, amounts and agent outputs "
    "on this screen are synthetic examples."
)


def render_agent_demo() -> None:
    """Choose a sample and member without running an investigation or fetching data."""

    st.title("Agent Evidence Brief")
    st.caption("Follow the evidence, understand its limits, and plan the next review.")
    st.warning(DEMO_NOTICE)
    st.caption(
        "Offline demonstration. No AWS services or Bedrock models are called. "
        "Full analysis days end on 16 Sep 2022."
    )

    results = sample_investigations()
    scenario = st.selectbox(
        "Sample scenario",
        options=list(results),
        format_func=SCENARIO_LABELS.__getitem__,
        key="agent_demo_scenario",
    )
    result = results[scenario]
    if scenario == "code_only":
        st.info("Code-only demonstration — fixed rules built this brief; this is not a fallback.")

    selected_txn_id = result["txn_id"]
    members = result["members"]
    if len(members) > 1:
        st.subheader("Grouped case")
        st.text(f"Case ID: {result['case_id'] or 'Not supplied'}")
        st.caption(f"{len(members)} members · Ordered by BQ1 rank")
        selected_txn_id = st.selectbox(
            "Case member",
            options=[member["alert"]["txn_id"] for member in members],
            # A scenario-specific widget cannot retain an ID from another scenario.
            key=f"agent_demo_member_{scenario}",
        )

    render_agent_brief(result, selected_txn_id=selected_txn_id)
