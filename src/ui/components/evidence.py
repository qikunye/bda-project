"""Resolve exact references from embedded member graphs and saved tool trace results."""

from dataclasses import dataclass

import streamlit as st

from src.ui.components.agent_graph import graph_transactions
from src.ui.components.transaction_detail import render_transaction_details


@dataclass
class EvidenceReference:
    kind: str
    member_id: str | None = None
    record: dict | None = None
    tool: str | None = None
    step: int | None = None
    context: dict | None = None


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
            return EvidenceReference("trace", record=record, tool=step["tool"], step=step["step"],
                                     context=step["result"])
    if any(member["graph"]["edges_capped"] for member in members):
        return EvidenceReference("limited")
    return EvidenceReference("unresolved")


def select_evidence(result: dict, evidence_id: str, state_prefix: str = "ui_batch") -> None:
    reference = resolve_evidence(result, evidence_id, st.session_state.get(f"{state_prefix}_member"))
    st.session_state[f"{state_prefix}_evidence"] = evidence_id
    if reference.kind == "graph":
        st.session_state[f"{state_prefix}_member"] = reference.member_id


def render_evidence_buttons(result: dict, ids: list[str], key_prefix: str, *, state_prefix: str = "ui_batch") -> None:
    st.caption("Supporting evidence · select a reference to inspect it")
    columns = st.columns(min(3, len(ids))) if ids else []
    for index, evidence_id in enumerate(ids):
        widget_prefix = "ui_evidence" if state_prefix == "ui_batch" else f"{state_prefix}_evidence"
        with columns[index % len(columns)]:
            st.button(evidence_id, key=f"{widget_prefix}_{key_prefix}_{index}",
                      on_click=select_evidence, args=(result, evidence_id, state_prefix), width="stretch")


def evidence_fields(reference: EvidenceReference) -> dict:
    """Map actual records, using explicit tool direction/account context only."""
    record = reference.record or {}
    fields = {name: record.get(name) for name in (
        "txn_id", "sender", "receiver", "amount", "currency", "ts", "payment_format",
    )}
    if reference.kind == "graph":
        role = record.get("role")
        fields["role"] = {"ALERT": "Flagged transaction", "UPSTREAM": "Upstream payment",
                          "DOWNSTREAM": "Downstream payment"}.get(role)
        gap = record.get("gap_minutes")
        fields["offset_minutes"] = -gap if role == "UPSTREAM" and gap is not None else gap
    elif reference.tool == "get_account_activity":
        context = reference.context or {}
        for direction in ("incoming", "outgoing"):
            if record in context.get(direction, []):
                fields["sender"] = record.get("counterparty") if direction == "incoming" else context.get("account")
                fields["receiver"] = context.get("account") if direction == "incoming" else record.get("counterparty")
                fields["role"] = f"Account activity · {direction} payment"
                fields["offset_minutes"] = record.get("offset_minutes")
                break
    elif reference.tool == "get_related_alerts":
        # A related-alert record supplies one account's role, not both endpoints.
        if record.get("role") in {"sender", "receiver"}:
            fields[record["role"]] = record.get("account")
        fields["role"] = "Related flagged transaction"
    return fields


def render_selected_evidence(result: dict, selected_member: str, *, state_prefix: str = "ui_batch") -> None:
    evidence_id = st.session_state.get(f"{state_prefix}_evidence")
    if not evidence_id:
        st.subheader("Selected transaction details")
        st.info("Select a supporting transaction under a finding to inspect its payment details here.")
        return
    reference = resolve_evidence(result, evidence_id, selected_member)
    if reference.record is not None:
        render_transaction_details(evidence_fields(reference))
    else:
        st.subheader("Selected supporting transaction")
        st.text(evidence_id)
    if reference.kind == "graph":
        st.caption("The exact transaction is highlighted in this member's graph: a thicker edge labelled Selected.")
    elif reference.kind == "trace":
        st.info("This evidence is outside the displayed money-flow graph. No graph edge has been added.")
        st.caption(f"Trace step {reference.step} · {reference.tool}")
    elif reference.kind == "limited":
        st.warning("Reference unresolved in embedded records. A graph is capped; the payment may be omitted. No edge was fabricated.")
    else:
        st.warning("Unresolved reference: no matching transaction details exist in this saved result.")
    findings = [finding for finding in result["findings"] if evidence_id in finding["evidence_ids"]]
    if findings:
        st.markdown("**Supports finding**")
        for finding in findings:
            st.text(finding["code"])
            st.caption(finding["sentence"])
    if reference.record is not None:
        with st.expander("Raw evidence details", expanded=False):
            st.json(reference.record)
