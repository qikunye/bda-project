"""Typed shapes of the agent tool outputs (PLAN.md 5.2, 18).

Every output carries ``ids``: each transaction and account ID it shows. The decision validator
(milestone 4) accepts only cited IDs that appear in some ``ids`` list from the same run.
"""

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
