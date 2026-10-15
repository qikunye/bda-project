"""Deterministic, JSON-friendly evidence derived from BQ2 case edges."""

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

from src.bq2.transforms import (
    SENSITIVITY_WINDOWS_HOURS,
    build_window_sensitivity,
)

CANDIDATE_RANKING_POLICY_VERSION = "support_count_gap_account_v1"
CANDIDATE_RANKING_POLICY_SEMANTICS = (
    "provisional_investigation_order_not_a_validated_risk_score"
)

REQUIRED_RAW_EDGE_COLUMNS = frozenset(
    {
        "edge_role",
        "transaction_id",
        "transaction_timestamp",
        "source_account_key",
        "target_account_key",
        "amount_paid",
        "payment_currency",
        "payment_format",
        "gap_minutes",
    }
)

_SUPPORTED_ROLES = frozenset({"ALERT", "UPSTREAM", "DOWNSTREAM"})
_ROLE_ORDER = {"UPSTREAM": 1, "ALERT": 2, "DOWNSTREAM": 3}


class EvidenceError(ValueError):
    """Base error for invalid BQ2 evidence input."""


class EvidenceSchemaError(EvidenceError):
    """Raised when required raw-edge columns are unavailable."""


class EvidenceIntegrityError(EvidenceError):
    """Raised when raw-edge values are malformed or inconsistent."""


@dataclass(frozen=True)
class _Edge:
    role: str
    transaction_id: str
    timestamp: datetime
    source_account: str
    target_account: str
    source_entity_id: str | None
    source_entity_name: str | None
    target_entity_id: str | None
    target_entity_name: str | None
    amount: Decimal | None
    amount_status: str
    currency: str | None
    payment_format: str | None
    gap_minutes: int | float


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()

    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceIntegrityError(f"Invalid {field_name} in BQ2 evidence.")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if _is_missing(value):
        return None
    return str(value).strip()


def _parse_timestamp(value: Any) -> datetime:
    if _is_missing(value):
        raise EvidenceIntegrityError("Missing transaction_timestamp in BQ2 evidence.")

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as error:
            raise EvidenceIntegrityError(
                "Invalid transaction_timestamp in BQ2 evidence."
            ) from error
    else:
        raise EvidenceIntegrityError(
            "Invalid transaction_timestamp in BQ2 evidence."
        )

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_amount(value: Any) -> tuple[Decimal | None, str]:
    if _is_missing(value):
        return None, "missing"
    if isinstance(value, bool):
        return None, "invalid"

    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None, "invalid"

    if not amount.is_finite():
        return None, "invalid"
    return amount, "valid"


def _parse_gap_minutes(value: Any) -> int | float:
    if _is_missing(value) or isinstance(value, bool):
        raise EvidenceIntegrityError("Invalid gap_minutes in BQ2 evidence.")

    try:
        gap = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise EvidenceIntegrityError("Invalid gap_minutes in BQ2 evidence.") from error

    if not gap.is_finite() or gap < 0:
        raise EvidenceIntegrityError("Invalid gap_minutes in BQ2 evidence.")
    if gap == gap.to_integral_value():
        return int(gap)
    return float(gap)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _prepare_edges(raw_edges: pd.DataFrame) -> list[_Edge]:
    if not isinstance(raw_edges, pd.DataFrame):
        raise TypeError("raw_edges must be a Pandas DataFrame.")

    missing_columns = REQUIRED_RAW_EDGE_COLUMNS - set(raw_edges.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise EvidenceSchemaError(f"Missing required raw-edge columns: {missing}.")

    edges = []
    for row in raw_edges.to_dict(orient="records"):
        role = _required_string(row.get("edge_role"), "edge_role")
        if role not in _SUPPORTED_ROLES:
            raise EvidenceIntegrityError(f"Unsupported BQ2 edge role: {role}.")

        amount, amount_status = _parse_amount(row.get("amount_paid"))
        edges.append(
            _Edge(
                role=role,
                transaction_id=_required_string(
                    row.get("transaction_id"),
                    "transaction_id",
                ),
                timestamp=_parse_timestamp(row.get("transaction_timestamp")),
                source_account=_required_string(
                    row.get("source_account_key"),
                    "source_account_key",
                ),
                target_account=_required_string(
                    row.get("target_account_key"),
                    "target_account_key",
                ),
                source_entity_id=_optional_string(row.get("source_entity_id")),
                source_entity_name=_optional_string(
                    row.get("source_entity_name")
                ),
                target_entity_id=_optional_string(row.get("target_entity_id")),
                target_entity_name=_optional_string(
                    row.get("target_entity_name")
                ),
                amount=amount,
                amount_status=amount_status,
                currency=_optional_string(row.get("payment_currency")),
                payment_format=_optional_string(row.get("payment_format")),
                gap_minutes=_parse_gap_minutes(row.get("gap_minutes")),
            )
        )

    _unique_transactions(edges)
    return edges


def _transaction_core(edge: _Edge) -> tuple[Any, ...]:
    return (
        edge.source_account,
        edge.target_account,
        edge.timestamp,
        edge.source_entity_id,
        edge.source_entity_name,
        edge.target_entity_id,
        edge.target_entity_name,
        edge.amount,
        edge.amount_status,
        edge.currency,
        edge.payment_format,
    )


def _unique_transactions(edges: list[_Edge]) -> dict[str, dict[str, Any]]:
    transactions: dict[str, dict[str, Any]] = {}

    for edge in edges:
        existing = transactions.get(edge.transaction_id)
        if existing is None:
            transactions[edge.transaction_id] = {
                "edge": edge,
                "roles": {edge.role},
                "closest_gap_minutes": edge.gap_minutes,
            }
            continue

        if _transaction_core(existing["edge"]) != _transaction_core(edge):
            raise EvidenceIntegrityError(
                "Conflicting rows found for transaction ID "
                f"{edge.transaction_id}."
            )
        existing["roles"].add(edge.role)
        existing["closest_gap_minutes"] = min(
            existing["closest_gap_minutes"],
            edge.gap_minutes,
        )

    return transactions


def _deduplicate_edges(edges: list[_Edge]) -> list[_Edge]:
    by_transaction: dict[str, _Edge] = {}
    for edge in edges:
        existing = by_transaction.get(edge.transaction_id)
        if existing is None or edge.gap_minutes < existing.gap_minutes:
            by_transaction[edge.transaction_id] = edge
    return sorted(
        by_transaction.values(),
        key=lambda edge: (edge.timestamp, edge.transaction_id),
    )


def _amount_summary(edges: list[_Edge]) -> dict[str, object]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    valid_count = 0
    missing_count = 0
    invalid_count = 0
    zero_count = 0
    valid_without_currency_count = 0

    for edge in edges:
        if edge.amount_status == "missing":
            missing_count += 1
        elif edge.amount_status == "invalid":
            invalid_count += 1
        else:
            valid_count += 1
            if edge.amount == 0:
                zero_count += 1
            if edge.currency is None:
                valid_without_currency_count += 1
            else:
                totals[edge.currency] += edge.amount

    return {
        "amount_totals_by_currency": {
            currency: _format_decimal(totals[currency])
            for currency in sorted(totals)
        },
        "valid_amount_count": valid_count,
        "missing_amount_count": missing_count,
        "invalid_amount_count": invalid_count,
        "zero_amount_count": zero_count,
        "valid_amount_without_currency_count": valid_without_currency_count,
    }


def _known_amount_total(edges: list[_Edge]) -> str | None:
    amounts = [edge.amount for edge in edges if edge.amount_status == "valid"]
    if not amounts:
        return None
    return _format_decimal(sum(amounts, Decimal(0)))


def _relationship_id(
    role: str,
    source_account: str,
    target_account: str,
    currency: str | None,
) -> str:
    canonical = json.dumps(
        {
            "currency": currency,
            "role": role,
            "source_account": source_account,
            "target_account": target_account,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"rel_{digest}"


def build_transaction_evidence(raw_edges: pd.DataFrame) -> list[dict[str, object]]:
    """Return one JSON-friendly record per unique source transaction."""

    transactions = _unique_transactions(_prepare_edges(raw_edges))
    records = []

    for transaction_id, transaction in sorted(
        transactions.items(),
        key=lambda item: (item[1]["edge"].timestamp, item[0]),
    ):
        edge = transaction["edge"]
        records.append(
            {
                "transaction_id": transaction_id,
                "edge_roles": sorted(
                    transaction["roles"],
                    key=_ROLE_ORDER.__getitem__,
                ),
                "source_account_key": edge.source_account,
                "target_account_key": edge.target_account,
                "source_entity_id": edge.source_entity_id,
                "source_entity_name": edge.source_entity_name,
                "target_entity_id": edge.target_entity_id,
                "target_entity_name": edge.target_entity_name,
                "transaction_timestamp": _format_timestamp(edge.timestamp),
                "amount": (
                    _format_decimal(edge.amount)
                    if edge.amount_status == "valid"
                    else None
                ),
                "amount_status": edge.amount_status,
                "currency": edge.currency,
                "payment_format": edge.payment_format,
                "closest_gap_minutes": transaction["closest_gap_minutes"],
            }
        )

    return records


def calculate_case_statistics(
    raw_edges: pd.DataFrame,
    alert_sender: str,
    alert_receiver: str,
) -> dict[str, object]:
    """Calculate case-level counts and currency-safe transaction totals."""

    alert_sender = _required_string(alert_sender, "alert_sender")
    alert_receiver = _required_string(alert_receiver, "alert_receiver")
    edges = _prepare_edges(raw_edges)
    transactions = _unique_transactions(edges)
    unique_edges = [transaction["edge"] for transaction in transactions.values()]

    transaction_ids_by_role = {
        role: {
            edge.transaction_id
            for edge in edges
            if edge.role == role
        }
        for role in _SUPPORTED_ROLES
    }
    neighbour_transaction_ids = (
        transaction_ids_by_role["UPSTREAM"]
        | transaction_ids_by_role["DOWNSTREAM"]
    ) - transaction_ids_by_role["ALERT"]

    alert_accounts = {alert_sender, alert_receiver}
    upstream_accounts = {
        edge.source_account
        for edge in edges
        if edge.role == "UPSTREAM"
    } - alert_accounts
    downstream_accounts = {
        edge.target_account
        for edge in edges
        if edge.role == "DOWNSTREAM"
    } - alert_accounts

    relationship_transactions: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for edge in edges:
        if edge.role in {"UPSTREAM", "DOWNSTREAM"}:
            relationship_transactions[
                (edge.role, edge.source_account, edge.target_account)
            ].add(edge.transaction_id)

    supporting_timestamps = [
        transactions[transaction_id]["edge"].timestamp
        for transaction_id in neighbour_transaction_ids
    ]
    payment_formats = {
        edge.payment_format
        for edge in unique_edges
        if edge.payment_format is not None
    }
    amount_summary = _amount_summary(unique_edges)

    return {
        "total_transactions_including_alert": len(transactions),
        "alert_transaction_count": len(transaction_ids_by_role["ALERT"]),
        "neighbour_transactions_excluding_alert": len(
            neighbour_transaction_ids
        ),
        "upstream_transaction_count": len(
            transaction_ids_by_role["UPSTREAM"]
        ),
        "downstream_transaction_count": len(
            transaction_ids_by_role["DOWNSTREAM"]
        ),
        "upstream_connected_account_count": len(upstream_accounts),
        "downstream_connected_account_count": len(downstream_accounts),
        "unique_connected_account_count": len(
            upstream_accounts | downstream_accounts
        ),
        "repeated_relationship_count": sum(
            len(transaction_ids) > 1
            for transaction_ids in relationship_transactions.values()
        ),
        "repeated_relationship_semantics": (
            "directional source-target pairs with more than one unique "
            "transaction; currency does not change pair identity"
        ),
        "distinct_payment_format_count": len(payment_formats),
        "payment_format_scope": "unique transactions including the alert",
        "earliest_supporting_transaction_timestamp": (
            _format_timestamp(min(supporting_timestamps))
            if supporting_timestamps
            else None
        ),
        "latest_supporting_transaction_timestamp": (
            _format_timestamp(max(supporting_timestamps))
            if supporting_timestamps
            else None
        ),
        "amount_aggregation_scope": (
            "unique transactions including the alert; totals contain only "
            "valid amounts and never combine currencies"
        ),
        **amount_summary,
    }


def build_relationship_evidence(
    raw_edges: pd.DataFrame,
) -> list[dict[str, object]]:
    """Map stable role/account/currency relationships to source transactions."""

    edges = _prepare_edges(raw_edges)
    groups: dict[tuple[str, str, str, str | None], list[_Edge]] = defaultdict(list)

    for edge in edges:
        groups[
            (
                edge.role,
                edge.source_account,
                edge.target_account,
                edge.currency,
            )
        ].append(edge)

    records = []
    for (role, source, target, currency), group_edges in sorted(
        groups.items(),
        key=lambda item: (
            _ROLE_ORDER[item[0][0]],
            item[0][1],
            item[0][2],
            item[0][3] or "",
        ),
    ):
        unique_edges = _deduplicate_edges(group_edges)
        amount_summary = _amount_summary(unique_edges)
        known_amount_total = _known_amount_total(unique_edges)
        amount_total_is_complete = (
            amount_summary["valid_amount_count"] == len(unique_edges)
        )

        records.append(
            {
                "relationship_id": _relationship_id(
                    role,
                    source,
                    target,
                    currency,
                ),
                "direction": role.lower(),
                "source_account_key": source,
                "target_account_key": target,
                "currency": currency,
                "transaction_count": len(unique_edges),
                "transaction_ids": [
                    edge.transaction_id for edge in unique_edges
                ],
                "transaction_timestamps": [
                    _format_timestamp(edge.timestamp) for edge in unique_edges
                ],
                "amount_total": (
                    known_amount_total if amount_total_is_complete else None
                ),
                "known_amount_total": known_amount_total,
                "amount_total_is_complete": amount_total_is_complete,
                "valid_amount_count": amount_summary["valid_amount_count"],
                "missing_amount_count": amount_summary["missing_amount_count"],
                "invalid_amount_count": amount_summary["invalid_amount_count"],
                "zero_amount_count": amount_summary["zero_amount_count"],
                "payment_formats": sorted(
                    {
                        edge.payment_format
                        for edge in unique_edges
                        if edge.payment_format is not None
                    }
                ),
                "closest_gap_minutes": min(
                    edge.gap_minutes for edge in unique_edges
                ),
            }
        )

    return records


def rank_candidate_accounts(
    candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Order candidates by support count, closest gap, then account key."""

    def ranking_key(candidate: dict[str, object]) -> tuple[int, float, str]:
        account_key = candidate.get("account_key")
        supporting_count = candidate.get("supporting_transaction_count")
        closest_gap = candidate.get("closest_gap_minutes")

        if not isinstance(account_key, str) or not account_key:
            raise EvidenceIntegrityError("Candidate has an invalid account_key.")
        if (
            not isinstance(supporting_count, int)
            or isinstance(supporting_count, bool)
            or supporting_count < 1
        ):
            raise EvidenceIntegrityError(
                "Candidate has an invalid supporting_transaction_count."
            )
        if (
            not isinstance(closest_gap, (int, float))
            or isinstance(closest_gap, bool)
            or not math.isfinite(closest_gap)
            or closest_gap < 0
        ):
            raise EvidenceIntegrityError(
                "Candidate has an invalid closest_gap_minutes."
            )
        return (-supporting_count, float(closest_gap), account_key)

    ranked = []
    for rank, candidate in enumerate(sorted(candidates, key=ranking_key), start=1):
        record = dict(candidate)
        record["candidate_rank"] = rank
        record["ranking_policy_version"] = CANDIDATE_RANKING_POLICY_VERSION
        record["ranking_policy_semantics"] = (
            CANDIDATE_RANKING_POLICY_SEMANTICS
        )
        record["ranking_inputs"] = {
            "supporting_transaction_count": record[
                "supporting_transaction_count"
            ],
            "closest_gap_minutes": record["closest_gap_minutes"],
            "account_key_tiebreaker": record["account_key"],
        }
        ranked.append(record)
    return ranked


def build_candidate_evidence(
    raw_edges: pd.DataFrame,
    alert_sender: str,
    alert_receiver: str,
) -> list[dict[str, object]]:
    """Build and deterministically rank observed external account candidates."""

    alert_sender = _required_string(alert_sender, "alert_sender")
    alert_receiver = _required_string(alert_receiver, "alert_receiver")
    edges = _prepare_edges(raw_edges)
    alert_accounts = {alert_sender, alert_receiver}
    candidate_edges: dict[str, list[_Edge]] = defaultdict(list)

    for edge in edges:
        if edge.role == "UPSTREAM":
            account_key = edge.source_account
        elif edge.role == "DOWNSTREAM":
            account_key = edge.target_account
        else:
            continue

        if account_key not in alert_accounts:
            candidate_edges[account_key].append(edge)

    candidates = []
    for account_key, supporting_edges in candidate_edges.items():
        unique_edges = _deduplicate_edges(supporting_edges)
        roles = {edge.role for edge in supporting_edges}
        upstream_ids = {
            edge.transaction_id
            for edge in supporting_edges
            if edge.role == "UPSTREAM"
        }
        downstream_ids = {
            edge.transaction_id
            for edge in supporting_edges
            if edge.role == "DOWNSTREAM"
        }
        relationship_transactions: dict[
            tuple[str, str, str], set[str]
        ] = defaultdict(set)
        currency_transaction_ids: dict[str, set[str]] = defaultdict(set)
        missing_currency_ids = set()

        for edge in supporting_edges:
            relationship_transactions[
                (edge.role, edge.source_account, edge.target_account)
            ].add(edge.transaction_id)
            if edge.currency is None:
                missing_currency_ids.add(edge.transaction_id)
            else:
                currency_transaction_ids[edge.currency].add(
                    edge.transaction_id
                )

        amount_summary = _amount_summary(unique_edges)
        relationship_ids = {
            _relationship_id(
                edge.role,
                edge.source_account,
                edge.target_account,
                edge.currency,
            )
            for edge in supporting_edges
        }

        if roles == {"UPSTREAM", "DOWNSTREAM"}:
            direction = "both"
        elif roles == {"UPSTREAM"}:
            direction = "upstream"
        else:
            direction = "downstream"

        candidates.append(
            {
                "account_key": account_key,
                "relationship_direction": direction,
                "supporting_transaction_count": len(unique_edges),
                "upstream_supporting_transaction_count": len(upstream_ids),
                "downstream_supporting_transaction_count": len(
                    downstream_ids
                ),
                "distinct_relationship_count": len(
                    relationship_transactions
                ),
                "relationship_ids": sorted(relationship_ids),
                "closest_gap_minutes": min(
                    edge.gap_minutes for edge in unique_edges
                ),
                "earliest_supporting_transaction_timestamp": _format_timestamp(
                    min(edge.timestamp for edge in unique_edges)
                ),
                "latest_supporting_transaction_timestamp": _format_timestamp(
                    max(edge.timestamp for edge in unique_edges)
                ),
                "transaction_ids": [
                    edge.transaction_id for edge in unique_edges
                ],
                "payment_formats": sorted(
                    {
                        edge.payment_format
                        for edge in unique_edges
                        if edge.payment_format is not None
                    }
                ),
                "transaction_counts_by_currency": {
                    currency: len(currency_transaction_ids[currency])
                    for currency in sorted(currency_transaction_ids)
                },
                "transactions_with_missing_currency_count": len(
                    missing_currency_ids
                ),
                "amount_totals_by_currency": amount_summary[
                    "amount_totals_by_currency"
                ],
                "amount_aggregation_scope": (
                    "valid supporting amounts only; missing and invalid "
                    "amount counts indicate completeness"
                ),
                "valid_amount_count": amount_summary["valid_amount_count"],
                "missing_amount_count": amount_summary[
                    "missing_amount_count"
                ],
                "invalid_amount_count": amount_summary[
                    "invalid_amount_count"
                ],
                "zero_amount_count": amount_summary["zero_amount_count"],
                "repeated_transfer_indicator": any(
                    len(transaction_ids) > 1
                    for transaction_ids in relationship_transactions.values()
                ),
                "evidence_semantics": (
                    "observed_transaction_relationship_not_a_risk_assessment"
                ),
            }
        )

    return rank_candidate_accounts(candidates)


def build_window_comparison(
    raw_edges: pd.DataFrame,
    alert_sender: str,
    alert_receiver: str,
    queried_window_hours: int,
) -> dict[str, object]:
    """Serialize existing sensitivity results without exceeding the horizon."""

    alert_sender = _required_string(alert_sender, "alert_sender")
    alert_receiver = _required_string(alert_receiver, "alert_receiver")
    _prepare_edges(raw_edges)
    if (
        not isinstance(queried_window_hours, int)
        or isinstance(queried_window_hours, bool)
        or queried_window_hours <= 0
    ):
        raise ValueError("queried_window_hours must be a positive integer.")

    sensitivity = build_window_sensitivity(
        raw_edges,
        alert_sender,
        alert_receiver,
        queried_window_hours,
    )

    windows = []
    previous_accounts = None
    previous_transactions = None

    for row in sensitivity.to_dict(orient="records"):
        connected_accounts = int(row["Connected accounts"])
        connected_transactions = int(row["Connected transaction edges"])
        windows.append(
            {
                "window_hours": int(row["Window (hours)"]),
                "upstream_account_count": int(row["Upstream accounts"]),
                "downstream_account_count": int(row["Downstream accounts"]),
                "unique_candidate_account_count": connected_accounts,
                "neighbour_transaction_edge_count": connected_transactions,
                "additional_accounts_from_previous_window": (
                    connected_accounts - previous_accounts
                    if previous_accounts is not None
                    else None
                ),
                "additional_transactions_from_previous_window": (
                    connected_transactions - previous_transactions
                    if previous_transactions is not None
                    else None
                ),
            }
        )
        previous_accounts = connected_accounts
        previous_transactions = connected_transactions

    unavailable_windows = [
        window
        for window in SENSITIVITY_WINDOWS_HOURS
        if window > queried_window_hours
    ]

    return {
        "queried_horizon_hours": queried_window_hours,
        "available_windows": windows,
        "unavailable_windows_hours": unavailable_windows,
        "requires_wider_query": bool(unavailable_windows),
        "neighbour_count_semantics": (
            "raw upstream/downstream edge rows, matching dashboard sensitivity"
        ),
        "recommended_window": None,
        "recommendation_policy_status": (
            "not_implemented_pending_validated_policy"
        ),
    }
