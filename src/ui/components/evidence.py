"""Resolve exact references from embedded member graphs and saved tool trace results."""

from dataclasses import dataclass

import streamlit as st

from src.ui.components.agent_graph import graph_transactions


@dataclass
class EvidenceReference:
    kind: str
    member_id: str | None = None
    record: dict | None = None
    tool: str | None = None
    step: int | None = None


def _matching_records(value, evidence_id):
    if isinstance(value, dict):
        if value.get("txn_id", value.get("transaction_id")) == evidence_id:
            yield value
        for item in value.values():
            yield from _matching_records(item, evidence_id)
    elif isinstance(value, list):
        for item in value:
            yield from _matching_records(item, evidence_id)


def resolve_evidence(result: dict, evidence_id: str, selected_member: str | None = None) -> EvidenceReference:
    members = sorted(result["members"], key=lambda member: member["alert"]["txn_id"] != selected_member)
    for member in members:
        for payment in graph_transactions(member):
            if payment["txn_id"] == evidence_id:
                return EvidenceReference("graph", member["alert"]["txn_id"], payment)
    for step in result["trace"]:
        record = next(_matching_records(step["result"], evidence_id), None)
        if record is not None:
            return EvidenceReference("trace", record=record, tool=step["tool"], step=step["step"])
    if any(member["graph"]["edges_capped"] for member in members):
        return EvidenceReference("limited")
    return EvidenceReference("unresolved")


def select_evidence(result: dict, evidence_id: str) -> None:
    reference = resolve_evidence(result, evidence_id, st.session_state.get("ui_batch_member"))
    st.session_state["ui_batch_evidence"] = evidence_id
    if reference.kind == "graph":
        st.session_state["ui_batch_member"] = reference.member_id


def render_evidence_buttons(result: dict, ids: list[str], key_prefix: str) -> None:
    st.caption("Supporting evidence · select a reference to inspect it")
    for index, evidence_id in enumerate(ids):
        st.button(evidence_id, key=f"ui_evidence_{key_prefix}_{index}",
                  on_click=select_evidence, args=(result, evidence_id))


def render_selected_evidence(result: dict, selected_member: str) -> None:
    evidence_id = st.session_state.get("ui_batch_evidence")
    if not evidence_id:
        return
    st.subheader("Supporting evidence")
    st.code(evidence_id, language=None)
    reference = resolve_evidence(result, evidence_id, selected_member)
    if reference.kind == "graph":
        st.caption("The exact transaction is highlighted in this member's graph.")
    elif reference.kind == "trace":
        st.info("This evidence is outside the displayed money-flow graph. Saved tool details are shown below.")
        st.caption(f"Trace step {reference.step} · {reference.tool}")
    elif reference.kind == "limited":
        st.warning("Reference unresolved in embedded records. A graph is capped; the payment may be omitted. No edge was fabricated.")
    else:
        st.warning("Unresolved reference: no matching transaction details exist in this saved result.")
    if reference.record is not None:
        st.json(reference.record)
