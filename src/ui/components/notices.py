"""Concise, shared interpretation limits for analyst workflows."""

import streamlit as st


def render_project_notices() -> None:
    with st.sidebar.expander("Data and interpretation limits", expanded=False):
        st.write("The IBM AML data is synthetic. Full analysis days end on 16 September 2022; "
                 "small later pipeline runs do not establish model effectiveness.")
        st.write("BQ1 scores support ranking, not calibrated laundering probabilities. Findings and connected "
                 "accounts are evidence to review, not proof of laundering. Empty graphs or findings do not establish innocence.")
        st.write("Agent observations are interpretations, not independently verified findings. Downstream windows "
                 "may be incomplete. Monetary totals must remain separate by currency.")
        st.write("Saved batch and on-demand briefs may use different evidence and cutoffs. Real-time Lambda "
                 "explanations are a separate product from the batch investigation agent's validated findings.")
