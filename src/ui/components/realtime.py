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


def threshold_status(record: RealtimeRecord) -> str:
    alert = record.alert
    if alert is None or alert.score is None or alert.threshold is None:
        return "Threshold unavailable"
    return "Meets scoring threshold" if alert.score >= alert.threshold else "Below scoring threshold"


def explanation_status(record: RealtimeRecord) -> str:
    if record.status == "available":
        if record.brief and record.brief.pipeline_status == "needs_review":
            return "Fallback / needs review"
        if record.brief and record.brief.narrative:
            return "Explanation available"
        return "Data unavailable / limited explanation"
    if record.status == "pending":
        return "Explanation pending"
    if record.status in {"alert_error", "brief_error"}:
        return "AWS read failure"
    return "Data unavailable"


def monitoring_rows(records: list[RealtimeRecord]) -> list[dict]:
    return [{
        "Transaction ID": record.txn,
        "Transaction timestamp": timestamp_text(record.alert.timestamp if record.alert else None),
        "Processing timestamp": timestamp_text(record.alert.scored_at if record.alert else None),
        "Sender": record.alert.sender if record.alert else None,
        "Receiver": record.alert.receiver if record.alert else None,
        "Amount": record.alert.amount if record.alert and record.alert.amount is not None else UNAVAILABLE,
        "Currency": record.alert.currency if record.alert and record.alert.currency else UNAVAILABLE,
        "BQ1 ranking score": record.alert.score if record.alert else None,
        "Threshold status": threshold_status(record),
        "Explanation status": explanation_status(record),
    } for record in records]


def render_realtime_record(record: RealtimeRecord, heading: str = "Selected real-time alert") -> None:
    st.subheader(heading)
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
            if alert.payment_format:
                st.text(f"Payment format: {alert.payment_format}")
            if alert.threshold is not None:
                st.text(f"Scoring threshold: {alert.threshold:g} · {threshold_status(record)}")
            st.caption("A BQ1 score supports ranking; it is not a calibrated laundering probability. "
                       "Amounts are not combined across currencies.")
            if any(value is not None for value in (alert.scored_at, alert.arrival_at, alert.model_version)):
                with st.expander("Scoring provenance", expanded=False):
                    st.text(f"Scorer model version: {alert.model_version or UNAVAILABLE}")
                    st.text(f"Scored at: {timestamp_text(alert.scored_at)}")
                    st.text(f"Kinesis arrival timestamp: {timestamp_text(alert.arrival_at)}")
                    if alert.ingest_to_score_ms is not None:
                        st.text(f"Reported ingest-to-score latency: {alert.ingest_to_score_ms:g} ms")
    if record.brief is not None:
        st.subheader("Real-time explanation")
        st.caption("Completed explanation record" if record.brief.content_mapped else "Explanation record has limited content")
        st.caption("Generated pipeline interpretation, not independently validated investigation-agent findings.")
        brief = record.brief
        if not brief.content_mapped:
            st.info("A brief record exists, but no supported explanation fields were stored. "
                    "It may be older or incomplete; no raw AWS record is displayed.")
        else:
            if brief.pipeline_status == "needs_review":
                st.warning("Explainer status: needs_review — the pipeline stored a template fallback after validation retries.")
            elif brief.pipeline_status is not None:
                st.caption(f"Explainer status: {brief.pipeline_status}. Schema/numeric grounding checks do not prove the interpretation is correct.")
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
            else:
                st.caption("Brief completion timestamp is not stored in DynamoDB; no completion time or queue latency is inferred.")
            if brief.model_version is not None:
                st.text(f"Explainer model version: {brief.model_version}")
            if brief.latency_ms is not None:
                st.text(f"Reported explainer execution latency: {brief.latency_ms:g} ms")
