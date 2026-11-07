"""Build is459_agent_decisions rows from one agent result."""

from src.agent.cases import Case
from src.agent.contracts import CaseRow, DecisionRow, InvestigateResult, Trigger


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
    rows.extend(
        pointer(txn_id, "member")
        for txn_id in dict.fromkeys(member_txn_ids)
        if txn_id != representative
    )
    return rows


def build_case_row(
    case: Case, result: InvestigateResult, *, run_id: str, created_at: str
) -> CaseRow:
    """The is459_agent_cases item for one case."""

    decision, stats = result["decision"], result["stats"]
    if result["status"] != "ok" or decision is None or stats is None:
        raise ValueError("Only a result with status 'ok' has a case row.")
    return CaseRow(
        day=case.txn_date,
        case_id=case.case_id,
        representative_txn_id=case.representative_txn_id,
        member_txn_ids=list(case.member_txn_ids),
        size=len(case.member_txn_ids),
        verdict=decision["verdict"],
        confidence=decision["confidence"],
        fallback=stats["fallback"],
        run_id=run_id,
        created_at=created_at,
        trace_key=trace_key(case.txn_date, case.representative_txn_id),
    )
