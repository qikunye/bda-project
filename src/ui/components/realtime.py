"""Present real-time DTOs independently of batch agent briefs and findings."""

from datetime import datetime, timezone

import streamlit as st

from src.ui.services.realtime import STATUS_LABELS, RealtimeRecord

UNAVAILABLE = "Unavailable / not mapped"


def timestamp_text(value: str | None) -> str:
    if value is None:
        return UNAVAILABLE
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return f"{value} (format unverified)"
    if parsed.tzinfo is None:
        return f"{value} (timezone unspecified)"
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def monitoring_rows(records: list[RealtimeRecord]) -> list[dict]:
    return [{
        "Transaction ID": record.txn,
        "Alert timestamp": timestamp_text(record.alert.timestamp if record.alert else None),
        "Amount": record.alert.amount if record.alert and record.alert.amount is not None else UNAVAILABLE,
        "Currency": record.alert.currency if record.alert and record.alert.currency else UNAVAILABLE,
        "BQ1 ranking score": record.alert.score if record.alert else None,
        "Explanation status": STATUS_LABELS[record.status],
    } for record in records]


def render_realtime_record(record: RealtimeRecord) -> None:
    st.subheader("Selected real-time alert")
    st.code(record.txn, language=None)
    st.text(f"Explanation status: {STATUS_LABELS[record.status]}")
    for failure in (record.alert_failure, record.brief_failure):
        if failure is not None:
            st.error(failure.message)
            st.caption(f"Debug reference: {failure.reference}")
    if record.status == "incompatible":
        st.error("The record or lookup key is incompatible with the documented minimal schema. "
                 "Confirm the txn key, any sort key and Lambda record fields before mapping more data.")
    elif record.status == "pending":
        st.info("The alert was found, but no brief was stored in this lookup. It may be pending or unavailable; "
                "no processing-failure marker is verified. Refresh explicitly to check again.")
    elif record.status == "not_found":
        st.info("The alert was not found in this lookup. This is not a permanent missing-record conclusion. "
                "Check the ID and refresh deliberately.")
    elif record.status == "unpaired":
        st.warning("A brief record was found without its matching alert in these separate reads. "
                   "Arrival order, retention and processing status cannot be inferred from this alone.")
    if record.detail:
        st.caption(record.detail)
    if record.alert is not None:
        with st.container(border=True):
            alert = record.alert
            st.text(f"Alert timestamp: {timestamp_text(alert.timestamp)}")
            st.text(f"Sender: {alert.sender or UNAVAILABLE}")
            st.text(f"Receiver: {alert.receiver or UNAVAILABLE}")
            amount, score = st.columns(2)
            amount.metric("Transaction amount", alert.amount if alert.amount is not None else UNAVAILABLE)
            score.metric("BQ1 score · ranking signal", f"{alert.score:.4f}" if alert.score is not None else UNAVAILABLE)
            st.text(f"Currency: {alert.currency or UNAVAILABLE}")
            st.caption("A BQ1 score supports ranking; it is not a calibrated laundering probability. "
                       "Amounts are not combined across currencies.")
    if record.brief is not None:
        st.subheader("Real-time explanation")
        st.caption("Generated pipeline interpretation, not independently validated investigation-agent findings.")
        brief = record.brief
        if not brief.content_mapped:
            st.info("A brief record exists, but narrative, next-check and typology field names are unverified. "
                    "Its content is not mapped; no raw AWS record is displayed.")
        else:
            if brief.narrative:
                st.write(brief.narrative)
            else:
                st.info("No narrative was supplied in this presentation record.")
            if brief.next_checks:
                st.markdown("**Suggested next checks**")
                for check in brief.next_checks:
                    st.markdown(f"- [ ] {check}")
            if brief.typology:
                st.text(f"Pipeline typology / interpretation: {brief.typology}")
            if brief.completed_at is not None:
                st.text(f"Brief completion timestamp: {timestamp_text(brief.completed_at)}")
