"""Defines the data shapes used across the agent: tool outputs, briefs and saved rows."""

from typing import Literal, TypedDict


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


class ReturnedToSource(TypedDict):
    """A downstream payment that goes back to where the money came from.

    ``account`` is either the alert's sender or the sender of an upstream payment. ``source_txn_id``
    is the alert itself in the first case, or the upstream payment closest to the alert in the second.
    """

    account: str
    kind: Literal["alert_sender", "upstream_source"]
    downstream_txn_id: str
    source_txn_id: str
    gap_minutes: int


class CurrencyFlow(TypedDict):
    """How much of the money that came in went on, in one currency. Never summed across currencies."""

    upstream_total: float
    downstream_total: float
    # Onward payments / the alert amount, in the alert's own currency only (null in other currencies
    # and when the alert has no amount). Counts only onward payments within
    # AGENT_PASS_THROUGH_MAX_GAP_MIN of the alert. Can be above 1.
    quick_onward_share: float | None
    # Minutes from the alert to the first and the median onward payment; null when none went on.
    fastest_onward_gap_minutes: int | None
    median_onward_gap_minutes: float | None
    fastest_onward_txn_id: str | None


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
    window_truncated: bool  # The window runs past the end of the dataset, not just a run's cutoff.
    horizon_end: str
    # Hours from the alert to the end of visible data, capped at the window. "No onward payment" only
    # means something if this is large enough.
    hours_observed_after_alert: float
    returned_to_source: list[ReturnedToSource]
    flow: dict[str, CurrencyFlow]
    ids: list[str]


class ActivityPayment(TypedDict):
    """One other payment of the account, seen from that account."""

    txn_id: str
    txn_date: str
    ts: str
    counterparty: str
    amount: float | None
    currency: str
    payment_format: str
    offset_minutes: int  # Negative before the alert, positive after it.


class DailyActivity(TypedDict):
    date: str
    n_incoming: int
    n_outgoing: int
    incoming_totals: dict[str, float]
    outgoing_totals: dict[str, float]


class AccountActivity(TypedDict):
    """Other payments of the alert's sender or receiver. Counts and totals use every payment;
    ``incoming`` and ``outgoing`` hold at most the cap, nearest the alert first."""

    txn_id: str
    side: Literal["sender", "receiver"]
    account: str
    window_h: int
    incoming: list[ActivityPayment]
    outgoing: list[ActivityPayment]
    n_incoming: int
    n_outgoing: int
    n_incoming_counterparties: int
    n_outgoing_counterparties: int
    per_currency_totals: dict[str, dict[str, float]]
    no_activity: bool
    edges_capped: bool
    window_truncated: bool  # The window runs past the end of the dataset, not just a run's cutoff.
    hours_observed_after_alert: float
    horizon_end: str
    # Whole days before the alert's day, from the first day the data covers. Days with no
    # payments count here but have no entry in daily_history.
    history_days: int
    daily_history: list[DailyActivity]
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


# --- Agent output format: an evidence brief, with no verdict.
# docs/agent_output_format.md explains each field for the app.

FindingCode = Literal[
    "PASS_THROUGH",
    "FAN_OUT",
    "FAN_IN",
    "MONEY_RETURNS",
    "NEIGHBOURS_HIGH_RISK",
    "REPEAT_ACCOUNT_ALERTS",
]
# Gaps are set by code from tool flags, never by the LLM.
GapCode = Literal[
    "NO_GRAPH_AVAILABLE", "TRUNCATED_WINDOW", "SHORT_OBSERVATION", "SHORT_HISTORY"
]
NextCheckCode = Literal[
    "CHECK_ONWARD_ACCOUNTS",
    "CHECK_SOURCE_ACCOUNTS",
    "CHECK_RETURN_FLOW",
    "REVIEW_RELATED_ALERTS",
    "CHECK_ACCOUNT_HISTORY",
    "RECHECK_AFTER_MORE_DATA",
]
# "valid" passed first time, "repaired" passed after the one repair, "fallback" used the code-only brief.
ValidationOutcome = Literal["valid", "repaired", "fallback"]
Trigger = Literal["B", "C"]  # B = batch, C = on demand.
RowRole = Literal["representative", "member"]


class Finding(TypedDict):
    """What the LLM submits: a pattern and the payments that show it."""

    code: FindingCode
    evidence_ids: list[str]


class Observation(TypedDict):
    """A remark the LLM wrote itself. Code checks the IDs and wording, not that it is right."""

    text: str  # At most AGENT_OBSERVATION_MAX_CHARS, no numbers, no accusations.
    evidence_ids: list[str]


class Brief(TypedDict):
    """What the LLM submits and code accepts. Gaps, money flow and sentences are added by code."""

    findings: list[Finding]
    next_checks: list[NextCheckCode]
    summary: str  # At most AGENT_SUMMARY_MAX_CHARS, no numbers.
    observations: list[Observation]  # Not verified by the finding rules. May be empty.


class RenderedFinding(TypedDict):
    code: FindingCode
    sentence: str  # Built by code from tool results. Show this.
    evidence_ids: list[str]


class RenderedGap(TypedDict):
    code: GapCode
    sentence: str


class RenderedNextCheck(TypedDict):
    code: NextCheckCode
    sentence: str


class MemberMoneyFlow(TypedDict):
    """Where one alert's money came from, where it went, how fast. Filled in by code only."""

    txn_id: str
    no_graph: bool
    n_upstream: int
    n_downstream: int
    per_currency: dict[str, CurrencyFlow]  # Never summed across currencies.
    hours_observed_after_alert: float


class TraceStep(TypedDict):
    step: int
    actor: Literal["code", "llm"]
    tool: str
    inputs: dict
    result: dict
    latency_ms: int


class RunStats(TypedDict):
    model: str
    steps: int  # Tool steps chosen by the LLM; at most AGENT_MAX_STEPS.
    tokens_in: int
    tokens_out: int
    latency_ms: int
    validation: ValidationOutcome
    repair_used: bool
    fallback: bool
    # Observations the validator left out of an accepted brief (wording, IDs, or a pattern the
    # findings do not have). They never cause a repair or a fallback.
    observations_dropped: int


class CaseMember(TypedDict):
    """One alert in the case and its payments, so the analyst needn't look them up."""

    alert: Alert
    graph: CaseGraph


class InvestigateResult(TypedDict):
    """What investigate(txn_id, txn_date, window_h) returns, and what a batch case stores."""

    status: Literal["ok", "not_found"]
    txn_id: str
    txn_date: str
    window_h: int
    alert: Alert
    # Set by code as case_<representative txn id> when the case has several alerts, else None.
    case_id: str | None
    summary: str | None  # None when status is "not_found".
    findings: list[RenderedFinding]
    gaps: list[RenderedGap]
    next_checks: list[RenderedNextCheck]
    # The LLM's own remarks. Empty when code alone built the brief. Show them as the agent's reading.
    observations: list[Observation]
    # One per member, best rank first. Empty when status is "not_found".
    money_flow: list[MemberMoneyFlow]
    members: list[CaseMember]
    fallback: bool  # True: code alone built the brief, not the LLM.
    stats: RunStats | None
    trace: list[TraceStep]


class DecisionRow(TypedDict, total=False):
    """One is459_agent_decisions item, keyed on transaction_id.

    Every row has the fields up to created_at. Only the representative row has the rest,
    so read the brief through representative_txn_id.
    """

    transaction_id: str
    day: str
    run_id: str
    trigger: Trigger
    role: RowRole
    case_id: str | None
    representative_txn_id: str
    created_at: str
    finding_codes: list[FindingCode]
    gap_codes: list[GapCode]
    next_check_codes: list[NextCheckCode]
    summary: str
    observation_count: int
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
    finding_codes: list[FindingCode]
    fallback: bool
    run_id: str
    created_at: str
    trace_key: str
