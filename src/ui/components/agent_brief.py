"""Render supplied evidence briefs; no retrieval, agent execution or analytical rules."""

from collections.abc import Callable

import streamlit as st

from src.agent.contracts import Alert, InvestigateResult, MemberMoneyFlow

UNAVAILABLE = "Unavailable / not applicable"


def _number(value: float | None, *, suffix: str = "", digits: int = 2) -> str:
    return UNAVAILABLE if value is None else f"{value:,.{digits}f}{suffix}"


def money_flow_rows(flow: MemberMoneyFlow) -> list[dict[str, str]]:
    """Format one row per supplied currency, preserving nulls and ratios above one."""

    return [
        {
            "Currency": currency,
            "Upstream total": _number(metrics["upstream_total"]),
            "Downstream total": _number(metrics["downstream_total"]),
            "Quick onward share": _number(metrics["quick_onward_share"], suffix="× alert amount"),
            "Fastest onward (minutes)": _number(metrics["fastest_onward_gap_minutes"], digits=0),
            "Median onward (minutes)": _number(metrics["median_onward_gap_minutes"]),
            "Fastest onward transaction": metrics["fastest_onward_txn_id"] or UNAVAILABLE,
        }
        for currency, metrics in flow["per_currency"].items()
    ]


def _references(ids: list[str]) -> None:
    if ids:
        st.caption("Supporting evidence IDs")
        st.code("\n".join(ids), language=None)


def render_selected_alert(alert: Alert, window_h: int) -> None:
    st.subheader("Selected alert")
    with st.container(border=True):
        st.text(f"Transaction ID: {alert['txn_id']}")
        left, right = st.columns(2)
        with left:
            st.text(f"Date: {alert.get('txn_date', UNAVAILABLE)}")
            st.text(f"Timestamp (UTC): {alert.get('ts', UNAVAILABLE)}")
            st.text(f"Sender: {alert.get('sender', UNAVAILABLE)}")
            st.text(f"Receiver: {alert.get('receiver', UNAVAILABLE)}")
        with right:
            amount = _number(alert.get("amount"))
            currency = alert.get("currency")
            st.text(f"Amount: {amount}{' ' + currency if currency else ''}")
            st.text(f"Currency: {currency or UNAVAILABLE}")
            st.text(f"Payment format: {alert.get('payment_format') or UNAVAILABLE}")
            st.text(f"Requested investigation window: ±{window_h} hours")

        score, day_rank, global_rank = st.columns(3)
        score.metric("BQ1 risk score · ranking signal", _number(alert.get("risk_score"), digits=4))
        day_rank.metric("BQ1 daily rank", _number(alert.get("risk_rank_day"), digits=0))
        global_rank.metric("BQ1 global rank", _number(alert.get("risk_rank_global"), digits=0))
        st.caption("The BQ1 score is a ranking signal, not a laundering probability.")


def render_findings(result: InvestigateResult, evidence_renderer: Callable | None = None) -> None:
    grouped = len(result["members"]) > 1
    st.subheader("Validated findings")
    scope = "Overall case" if grouped else "Selected alert"
    st.caption(f"{scope} · {len(result['findings'])} supported findings")
    if grouped:
        st.info("These findings belong to the overall case, not necessarily the selected member.")
    if not result["findings"]:
        st.info("No supported finding was identified in the available evidence.")
    for index, finding in enumerate(result["findings"]):
        with st.container(border=True):
            st.markdown(f"**{finding['code'].replace('_', ' ').capitalize()}**")
            st.write(finding["sentence"])
            if evidence_renderer is None:
                _references(finding["evidence_ids"])
            else:
                evidence_renderer(finding["evidence_ids"], f"finding_{index}")
    st.caption("Findings are evidence for review, not proof of laundering.")


def render_gaps_and_checks(result: InvestigateResult) -> None:
    st.subheader("Evidence gaps")
    if not result["gaps"]:
        st.caption("No evidence gaps were reported in this brief.")
    for gap in result["gaps"]:
        st.warning(gap["sentence"])

    st.subheader("Recommended next checks")
    if not result["next_checks"]:
        st.caption("No next checks were supplied.")
    for check in result["next_checks"]:
        st.markdown(f"- [ ] {check['sentence']}")


def render_observations(result: InvestigateResult) -> None:
    st.subheader("Agent observations — interpretation not independently verified")
    st.caption("IDs and wording are checked; the interpretation is not validated by finding rules.")
    if not result["observations"]:
        st.caption("No agent observations were supplied.")
    for observation in result["observations"]:
        with st.container(border=True):
            st.write(observation["text"])
            _references(observation["evidence_ids"])


def render_money_flow(flow: MemberMoneyFlow | None) -> None:
    st.subheader("Selected member money flow")
    if flow is None:
        st.info("Money-flow metrics are unavailable for this transaction.")
        return
    st.code(flow["txn_id"], language=None)
    incoming, outgoing, observed = st.columns(3)
    incoming.metric("Upstream transactions", flow["n_upstream"])
    outgoing.metric("Downstream transactions", flow["n_downstream"])
    observed.metric("Hours observed after alert", _number(flow["hours_observed_after_alert"]))
    if flow["no_graph"]:
        st.info("No payments into the sender or out of the receiver are available in this window.")
    rows = money_flow_rows(flow)
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption("No per-currency metrics are available.")
    st.caption(
        "Totals are kept separate by currency. Quick onward share is a multiple of the alert "
        "amount for the configured quick-payment interval; it can exceed 1. It is not a "
        "laundering percentage. These flows do not establish that the same funds moved."
    )


def render_provenance(result: InvestigateResult) -> None:
    stats = result["stats"]
    if stats is not None:
        if result["fallback"]:
            st.warning("Code-only fallback — fixed rules produced the brief after LLM failure.")
        elif stats["validation"] == "repaired":
            st.info("Accepted after repair — the brief passed after one retry.")
        with st.expander("How this brief was generated", expanded=False):
            st.caption("Execution metadata supplied with this result.")
            st.json(stats)

    with st.expander("Tool trace", expanded=False):
        if not result["trace"]:
            st.caption("No trace steps were supplied.")
        for step in result["trace"]:
            with st.container(border=True):
                st.text(
                    f"Step {step['step']} · {step['actor']} · {step['tool']} "
                    f"· {step['latency_ms']:,} ms"
                )
                st.caption("Inputs")
                st.json(step["inputs"])
                st.caption("Result")
                st.json(step["result"])


def render_agent_brief(
    result: InvestigateResult, *, selected_txn_id: str | None = None,
    show_alert: bool = True, evidence_renderer: Callable | None = None,
) -> None:
    """Present an existing result and a member's metrics without changing either."""

    if result["status"] == "not_found":
        st.info("Transaction not found. No evidence brief is available for this request.")
        st.code(result["txn_id"], language=None)
        return

    selected_txn_id = selected_txn_id or result["txn_id"]
    member = next(
        (m for m in result["members"] if m["alert"]["txn_id"] == selected_txn_id), None
    )
    if member is None:
        st.warning("The selected transaction is not a member of this result.")
        return
    if show_alert:
        render_selected_alert(member["alert"], result["window_h"])
    st.subheader("Agent evidence brief")
    if result["summary"]:
        st.write(result["summary"])
    render_findings(result, evidence_renderer)
    render_gaps_and_checks(result)
    render_observations(result)
    st.divider()
    flow = next((f for f in result["money_flow"] if f["txn_id"] == selected_txn_id), None)
    render_money_flow(flow)
    render_provenance(result)
