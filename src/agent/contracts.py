"""Defines the data shapes used across the agent: tool outputs, decisions and saved rows."""

from typing import Literal, TypedDict


class Reason(TypedDict):
    feature: str
    sentence: str
    direction: Literal["up", "down"]
    contribution: float


class Alert(TypedDict, total=False):
    found: bool
    txn_id: str
    txn_date: str
    ts: str
    sender: str
    receiver: str
    amount: float | None
    currency: str | None
    payment_format: str | None
    risk_score: float
    risk_rank_global: int
    risk_rank_day: int | None
    in_queue: bool
    in_day_top_k: bool
    reasons: list[Reason] | None
    ids: list[str]


class Edge(TypedDict):
    txn_id: str
    txn_date: str
    ts: str
    sender: str
    receiver: str
    amount: float
    currency: str
    payment_format: str
    gap_minutes: int


class CaseGraph(TypedDict):
    txn_id: str
    window_h: int
    upstream: list[Edge]
    downstream: list[Edge]
    n_upstream: int
    n_downstream: int
    n_upstream_accounts: int
    n_downstream_accounts: int
    per_currency_totals: dict[str, dict[str, float]]
    no_graph: bool
    edges_capped: bool
    window_truncated: bool
    horizon_end: str
    ids: list[str]


class NeighbourScore(TypedDict):
    txn_id: str
    txn_date: str
    scored: bool
    risk_score: float | None
    risk_rank_global: int | None
    risk_rank_day: int | None
    in_queue: bool
    in_day_top_k: bool
    has_reasons: bool


class NeighbourScores(TypedDict):
    neighbours: list[NeighbourScore]
    ids: list[str]


class RelatedAlert(TypedDict):
    txn_id: str
    txn_date: str
    ts: str
    risk_rank_day: int
    account: str
    role: Literal["sender", "receiver"]


class RelatedAlerts(TypedDict):
    alerts: list[RelatedAlert]
    top_k: int
    window_h: int
    ids: list[str]


# --- Agent output format (PLAN.md 6.3, 10, 19). Streamlit builds against these types.

# raise: show the analyst this case. dont_raise: move it down the agent's list (it never leaves the BQ1
# queue). not_sure: leave it where BQ1 ranks it. Related alerts are grouped by code, not by a verdict.
Verdict = Literal["raise", "dont_raise", "not_sure"]
Confidence = Literal["low", "medium", "high"]
ClaimCode = Literal[
    "REASONS_CORROBORATED",
    "NEIGHBOURS_HIGH_RISK",
    "FAST_CHAIN",
    "REPEAT_ACCOUNT_ALERTS",
    "REASONS_NOT_SUPPORTED",
    "NO_GRAPH_AVAILABLE",
    "TRUNCATED_WINDOW",
    "NO_REASONS_AVAILABLE",
]
# "valid" passed first time, "repaired" passed after the one repair, "fallback" used S1.
ValidationOutcome = Literal["valid", "repaired", "fallback"]
Trigger = Literal["B", "C"]  # B = batch, C = on demand (PLAN.md section 4).
RowRole = Literal["representative", "member"]


class Claim(TypedDict):
    code: ClaimCode
    evidence_ids: list[str]


class Decision(TypedDict):
    verdict: Verdict
    confidence: Confidence
    case_id: (
        str | None
    )  # Set by code as case_<representative txn id>, never by the LLM.
    claims: list[Claim]
    note: str | None


class RenderedClaim(TypedDict):
    code: ClaimCode
    sentence: (
        str  # Built by claims.py from tool results. Show this, not the LLM's note.
    )
    evidence_ids: list[str]


class TraceStep(TypedDict):
    step: int
    actor: Literal["code", "llm"]
    tool: str
    inputs: dict
    result: dict
    latency_ms: int


class RunStats(TypedDict):
    model: str
    steps: int  # Tool steps chosen by the LLM; at most 5 (PLAN.md rule 6).
    tokens_in: int
    tokens_out: int
    latency_ms: int
    validation: ValidationOutcome
    repair_used: bool
    fallback: bool


class CaseMember(TypedDict):
    """One alert in the case and its payments, so the analyst needn't look them up."""

    alert: Alert
    graph: CaseGraph


class InvestigateResult(TypedDict):
    """What investigate(txn_id, txn_date, window_h) returns."""

    status: Literal["ok", "not_found"]
    txn_id: str
    txn_date: str
    window_h: int
    alert: Alert
    # None when status is "not_found".
    decision: Decision | None
    rendered_claims: list[RenderedClaim]
    # None when that tool did not run in this investigation.
    graph: CaseGraph | None
    neighbours: NeighbourScores | None
    related: RelatedAlerts | None
    # Every alert in the case, best rank first. Empty when status is "not_found".
    members: list[CaseMember]
    stats: RunStats | None
    trace: list[TraceStep]


class DecisionRow(TypedDict, total=False):
    """One is459_agent_decisions item, keyed on transaction_id (PLAN.md D16, D24).

    Every row has the fields up to created_at. Only the representative row has the rest,
    so read claims and stats through representative_txn_id.
    """

    transaction_id: str
    day: str
    run_id: str
    trigger: Trigger
    role: RowRole
    verdict: Verdict
    case_id: str | None
    representative_txn_id: str
    created_at: str
    confidence: Confidence
    claims: list[Claim]
    note: str | None
    fallback: bool
    repair_used: bool
    steps: int
    tokens_in: int
    tokens_out: int
    latency_ms: int
    model: str
    trace_key: str


class CaseRow(TypedDict):
    """One is459_agent_cases item, keyed on day + case_id. Every case in the batch has one."""

    day: str
    case_id: str  # Always case_<representative txn id>, even for a single-alert case.
    representative_txn_id: str
    member_txn_ids: list[str]  # Best rank first, starting with the representative.
    size: int
    verdict: Verdict
    confidence: Confidence
    fallback: bool
    run_id: str
    created_at: str
    trace_key: str
