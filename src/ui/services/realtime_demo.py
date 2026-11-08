"""Invented real-time presentation data, not a claimed Lambda wire schema."""

from src.ui.services.errors import ServiceFailure
from src.ui.services.realtime import (
    AlertSummary,
    Explanation,
    RealtimeRecord,
    validate_ids,
)

DEMO_GROUPS = {
    "Sample session: mixed alerts": ["demo_rt_complete", "demo_rt_pending", "demo_rt_optional"],
    "Fallback / needs review": ["demo_rt_fallback"],
    "Completed explanation": ["demo_rt_complete"],
    "Explanation pending": ["demo_rt_pending"],
    "Missing alert": ["demo_rt_missing"],
    "Missing optional fields": ["demo_rt_optional"],
    "Alert read failure": ["demo_rt_alert_error"],
    "Brief read failure": ["demo_rt_brief_error"],
    "Limited narrative": ["demo_rt_limited"],
    "Unpaired brief record": ["demo_rt_unpaired"],
    "Incompatible record": ["demo_rt_incompatible"],
    "All sample states": ["demo_rt_complete", "demo_rt_pending", "demo_rt_missing", "demo_rt_optional",
                          "demo_rt_alert_error", "demo_rt_brief_error", "demo_rt_limited",
                          "demo_rt_unpaired", "demo_rt_incompatible", "demo_rt_fallback"],
}


def demo_records() -> dict[str, RealtimeRecord]:
    def alert(txn, **kwargs):
        return AlertSummary(txn, amount="12500.00", timestamp="2026-11-08T01:00:00+00:00",
                            sender="100|DEMO_RT_A", receiver="200|DEMO_RT_B", currency="USD", **kwargs)
    def explanation(txn, **kwargs):
        return Explanation(txn, content_mapped=True, **kwargs)
    def failure(side):
        return ServiceFailure("Simulated read failure", f"Simulated {side} read failure. Refresh explicitly to retry.",
                              f"demo-{side}-read", RuntimeError("Invented offline failure"))
    return {
        "demo_rt_complete": RealtimeRecord(
            "demo_rt_complete", "available", alert("demo_rt_complete", score=0.73, threshold=0.5, scored_at="2026-11-08T01:01:00+00:00"),
            explanation("demo_rt_complete", narrative="Simulated explanation: review the payment in its account context.",
                        next_checks=("Review the sender's recent payments.", "Check the counterparty details."),
                        typology="Illustrative pass-through hypothesis",
                        completed_at="2026-11-08T01:02:00+00:00", pipeline_status="ok"),
        ),
        "demo_rt_fallback": RealtimeRecord("demo_rt_fallback", "available", alert("demo_rt_fallback"),
            explanation("demo_rt_fallback", narrative="Simulated template explanation for manual review.",
                        pipeline_status="needs_review", next_checks=("Review the original payment.",))),
        "demo_rt_pending": RealtimeRecord("demo_rt_pending", "pending", alert("demo_rt_pending", score=0.31, threshold=0.5)),
        "demo_rt_missing": RealtimeRecord("demo_rt_missing", "not_found"),
        "demo_rt_optional": RealtimeRecord("demo_rt_optional", "pending", AlertSummary("demo_rt_optional")),
        "demo_rt_alert_error": RealtimeRecord("demo_rt_alert_error", "alert_error", alert_failure=failure("alert")),
        "demo_rt_brief_error": RealtimeRecord("demo_rt_brief_error", "brief_error", alert("demo_rt_brief_error"),
                                              brief_failure=failure("brief")),
        "demo_rt_limited": RealtimeRecord("demo_rt_limited", "available", alert("demo_rt_limited"),
                                          explanation("demo_rt_limited", next_checks=("Review the underlying payment.",))),
        "demo_rt_unpaired": RealtimeRecord("demo_rt_unpaired", "unpaired", brief=explanation(
            "demo_rt_unpaired", narrative="An invented brief exists without its matching alert in this lookup.")),
        "demo_rt_incompatible": RealtimeRecord("demo_rt_incompatible", "incompatible",
                                               detail="Invented incompatible-record example; no raw record is displayed."),
    }


class DemoRealtimeService:
    def lookup_many(self, txn_ids: list[str]) -> list[RealtimeRecord]:
        records = demo_records()
        return [records.get(txn, RealtimeRecord(txn, "not_found")) for txn in validate_ids(txn_ids)]
