"""Build is459_agent_decisions rows from one agent result."""

from src.agent.contracts import DecisionRow, InvestigateResult, Trigger


def trace_key(day: str, txn_id: str) -> str:
    return f"agent/traces/{day}/{txn_id}.json"


def build_decision_rows(
    result: InvestigateResult,
    *,
    run_id: str,
    created_at: str,
    trigger: Trigger = "B",
    member_txn_ids: tuple[str, ...] | list[str] = (),
) -> list[DecisionRow]:
    """One full row for the representative, and a short pointer row for each other transaction.

    Rows hold no Python floats, because DynamoDB rejects them.
    """

    decision, stats = result["decision"], result["stats"]
    if result["status"] != "ok" or decision is None or stats is None:
        raise ValueError("Only a result with status 'ok' has decision rows.")

    representative = result["txn_id"]
    day = result["txn_date"]

    def pointer(txn_id: str, role: str) -> DecisionRow:
        return DecisionRow(
            transaction_id=txn_id,
            day=day,
            run_id=run_id,
            trigger=trigger,
            role=role,
            verdict=decision["verdict"],
            case_id=decision["case_id"],
            representative_txn_id=representative,
            created_at=created_at,
        )

    rows = [
        DecisionRow(
            **pointer(representative, "representative"),
            confidence=decision["confidence"],
            claims=decision["claims"],
            promoted_txn_ids=decision["promoted_txn_ids"],
            note=decision["note"],
            fallback=stats["fallback"],
            repair_used=stats["repair_used"],
            steps=stats["steps"],
            tokens_in=stats["tokens_in"],
            tokens_out=stats["tokens_out"],
            latency_ms=stats["latency_ms"],
            model=stats["model"],
            trace_key=trace_key(day, representative),
        )
    ]
    seen = {representative}
    for role, txn_ids in (
        ("member", member_txn_ids),
        ("promoted", decision["promoted_txn_ids"]),
    ):
        for txn_id in txn_ids:
            if txn_id not in seen:
                seen.add(txn_id)
                rows.append(pointer(txn_id, role))
    return rows
