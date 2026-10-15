"""Semantic validation and strict JSON serialization for CaseContext payloads."""

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.bq2.evidence import (
    CANDIDATE_RANKING_POLICY_SEMANTICS,
    CANDIDATE_RANKING_POLICY_VERSION,
)

CASE_CONTEXT_SCHEMA_VERSION = "1.0"
_FULL_TOP_LEVEL_KEYS = {
    "schema_version",
    "alert",
    "bq1",
    "bq2",
    "data_quality",
    "provenance",
}
_FORBIDDEN_GROUND_TRUTH_KEYS = {
    "data_split",
    "evaluation_is_laundering",
    "is_laundering",
    "laundering_pattern",
    "laundering_pattern_id",
    "pattern_type",
}
_BQ1_STATUSES = {"available", "not_found", "unavailable"}


class CaseContextError(ValueError):
    """Base exception for invalid CaseContext data."""


class CaseContextIntegrityError(CaseContextError):
    """Raised when evidence sections are malformed or inconsistent."""


class CaseContextSerializationError(CaseContextError):
    """Raised when a context cannot be represented as strict JSON."""


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CaseContextIntegrityError(f"{field_name} must be an object.")
    return value


def _require_list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise CaseContextIntegrityError(f"{field_name} must be a list.")
    return value


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise CaseContextIntegrityError(
            f"{field_name} must be a non-negative integer."
        )
    return value


def _parse_utc_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise CaseContextIntegrityError(f"{field_name} must be a UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CaseContextIntegrityError(
            f"{field_name} must be a valid UTC timestamp."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise CaseContextIntegrityError(f"{field_name} must be UTC.")
    return parsed


def _validate_json_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CaseContextSerializationError(
                f"Non-finite number at {path} is not valid JSON."
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CaseContextSerializationError(
                    f"Non-string object key at {path}."
                )
            _validate_json_value(item, f"{path}.{key}")
        return
    raise CaseContextSerializationError(
        f"Unsupported value of type {type(value).__name__} at {path}."
    )


def _find_forbidden_keys(value: Any, path: str = "$") -> list[str]:
    found = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key.lower() in _FORBIDDEN_GROUND_TRUTH_KEYS:
                found.append(child_path)
            found.extend(_find_forbidden_keys(child, child_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            found.extend(_find_forbidden_keys(child, f"{path}[{index}]"))
    return found


def _validate_bq1(context: Mapping[str, Any]) -> None:
    bq1 = _require_mapping(context.get("bq1"), "bq1")
    status = bq1.get("status")
    if status not in _BQ1_STATUSES:
        raise CaseContextIntegrityError("bq1.status is invalid.")

    metadata = bq1.get("metadata")
    if status == "available" and not isinstance(metadata, Mapping):
        raise CaseContextIntegrityError(
            "Available BQ1 context requires metadata."
        )
    if status != "available" and metadata is not None:
        raise CaseContextIntegrityError(
            "Non-available BQ1 context cannot contain metadata."
        )

    explanations = _require_mapping(
        bq1.get("model_explanations"),
        "bq1.model_explanations",
    )
    if (
        explanations.get("status") != "unavailable"
        or explanations.get("explanations") is not None
    ):
        raise CaseContextIntegrityError(
            "Unverified BQ1 model explanations must remain explicitly unavailable."
        )

    if isinstance(metadata, Mapping):
        alert = context["alert"]
        for key in (
            "transaction_id",
            "source_account_key",
            "target_account_key",
        ):
            if metadata.get(key) != alert.get(key):
                raise CaseContextIntegrityError(
                    f"BQ1 metadata {key} does not match alert."
                )
        metadata_timestamp = _parse_utc_timestamp(
            f"{metadata.get('transaction_timestamp')}Z",
            "bq1.metadata.transaction_timestamp",
        )
        alert_timestamp = _parse_utc_timestamp(
            alert.get("transaction_timestamp"),
            "alert.transaction_timestamp",
        )
        if metadata_timestamp != alert_timestamp:
            raise CaseContextIntegrityError(
                "BQ1 metadata transaction_timestamp does not match alert."
            )


def _transaction_map(
    transactions: list[Any],
) -> dict[str, Mapping[str, Any]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw_transaction in enumerate(transactions):
        transaction = _require_mapping(
            raw_transaction,
            f"bq2.transactions[{index}]",
        )
        transaction_id = transaction.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            raise CaseContextIntegrityError(
                "Every transaction requires a non-empty transaction_id."
            )
        if transaction_id in by_id:
            raise CaseContextIntegrityError(
                f"Duplicate transaction evidence ID: {transaction_id}."
            )
        _require_list(transaction.get("edge_roles"), "transaction.edge_roles")
        _parse_utc_timestamp(
            transaction.get("transaction_timestamp"),
            "transaction.transaction_timestamp",
        )
        by_id[transaction_id] = transaction
    return by_id


def _validate_alert(
    alert: Mapping[str, Any],
    transactions: Mapping[str, Mapping[str, Any]],
) -> str:
    alert_transaction_ids = [
        transaction_id
        for transaction_id, transaction in transactions.items()
        if "ALERT" in transaction["edge_roles"]
    ]
    if len(alert_transaction_ids) != 1:
        raise CaseContextIntegrityError(
            "Context must contain exactly one ALERT transaction."
        )
    anchor_id = alert_transaction_ids[0]
    if alert.get("transaction_id") != anchor_id:
        raise CaseContextIntegrityError(
            "Alert transaction_id does not match the ALERT evidence record."
        )
    anchor = transactions[anchor_id]
    for key in (
        "source_account_key",
        "target_account_key",
        "transaction_timestamp",
        "amount",
        "amount_status",
        "currency",
        "payment_format",
    ):
        if alert.get(key) != anchor.get(key):
            raise CaseContextIntegrityError(
                f"Alert {key} does not match the ALERT evidence record."
            )
    parsed_timestamp = _parse_utc_timestamp(
        alert.get("transaction_timestamp"),
        "alert.transaction_timestamp",
    )
    if parsed_timestamp.second != 0 or parsed_timestamp.microsecond != 0:
        raise CaseContextIntegrityError(
            "Alert timestamp must use minute-resolution source precision."
        )
    return anchor_id


def _amount_counts(
    transactions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    counts = {
        "valid_amount_count": 0,
        "missing_amount_count": 0,
        "invalid_amount_count": 0,
        "zero_amount_count": 0,
        "valid_amount_without_currency_count": 0,
    }
    totals: dict[str, Decimal] = {}
    for transaction in transactions.values():
        status = transaction.get("amount_status")
        amount = transaction.get("amount")
        currency = transaction.get("currency")
        if status not in {"valid", "missing", "invalid"}:
            raise CaseContextIntegrityError("Transaction amount_status is invalid.")
        counts[f"{status}_amount_count"] += 1
        if status != "valid":
            if amount is not None:
                raise CaseContextIntegrityError(
                    "Missing or invalid transaction amounts must be null."
                )
            continue
        if not isinstance(amount, str):
            raise CaseContextIntegrityError(
                "Valid financial amounts must be represented as strings."
            )
        try:
            decimal_amount = Decimal(amount)
        except InvalidOperation as error:
            raise CaseContextIntegrityError(
                "Valid transaction amount is not a decimal string."
            ) from error
        if not decimal_amount.is_finite():
            raise CaseContextIntegrityError(
                "Valid transaction amount must be finite."
            )
        if decimal_amount == 0:
            counts["zero_amount_count"] += 1
        if currency is None:
            counts["valid_amount_without_currency_count"] += 1
        elif isinstance(currency, str) and currency:
            totals[currency] = totals.get(currency, Decimal(0)) + decimal_amount
        else:
            raise CaseContextIntegrityError("Transaction currency is invalid.")
    counts["amount_totals_by_currency"] = {
        currency: format(totals[currency], "f") for currency in sorted(totals)
    }
    return counts


def _validate_statistics(
    context: Mapping[str, Any],
    transactions: Mapping[str, Mapping[str, Any]],
) -> None:
    bq2 = context["bq2"]
    statistics = _require_mapping(
        bq2.get("case_statistics"),
        "bq2.case_statistics",
    )
    alert = context["alert"]
    roles = {
        role: {
            transaction_id
            for transaction_id, transaction in transactions.items()
            if role in transaction["edge_roles"]
        }
        for role in ("ALERT", "UPSTREAM", "DOWNSTREAM")
    }
    alert_accounts = {
        alert["source_account_key"],
        alert["target_account_key"],
    }
    upstream_accounts = {
        transaction["source_account_key"]
        for transaction in transactions.values()
        if "UPSTREAM" in transaction["edge_roles"]
    } - alert_accounts
    downstream_accounts = {
        transaction["target_account_key"]
        for transaction in transactions.values()
        if "DOWNSTREAM" in transaction["edge_roles"]
    } - alert_accounts
    relationship_transactions: dict[tuple[str, str, str], set[str]] = (
        defaultdict(set)
    )
    for transaction_id, transaction in transactions.items():
        for role in ("UPSTREAM", "DOWNSTREAM"):
            if role in transaction["edge_roles"]:
                relationship_transactions[
                    (
                        role,
                        transaction["source_account_key"],
                        transaction["target_account_key"],
                    )
                ].add(transaction_id)
    supporting_ids = (roles["UPSTREAM"] | roles["DOWNSTREAM"]) - roles["ALERT"]
    supporting_timestamps = [
        _parse_utc_timestamp(
            transactions[transaction_id]["transaction_timestamp"],
            "transaction.transaction_timestamp",
        )
        for transaction_id in supporting_ids
    ]
    payment_formats = {
        transaction["payment_format"]
        for transaction in transactions.values()
        if transaction.get("payment_format") is not None
    }
    expected = {
        "total_transactions_including_alert": len(transactions),
        "alert_transaction_count": len(roles["ALERT"]),
        "neighbour_transactions_excluding_alert": len(
            (roles["UPSTREAM"] | roles["DOWNSTREAM"]) - roles["ALERT"]
        ),
        "upstream_transaction_count": len(roles["UPSTREAM"]),
        "downstream_transaction_count": len(roles["DOWNSTREAM"]),
        "upstream_connected_account_count": len(upstream_accounts),
        "downstream_connected_account_count": len(downstream_accounts),
        "unique_connected_account_count": len(
            upstream_accounts | downstream_accounts
        ),
        "repeated_relationship_count": sum(
            len(transaction_ids) > 1
            for transaction_ids in relationship_transactions.values()
        ),
        "distinct_payment_format_count": len(payment_formats),
        "earliest_supporting_transaction_timestamp": (
            min(supporting_timestamps)
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            if supporting_timestamps
            else None
        ),
        "latest_supporting_transaction_timestamp": (
            max(supporting_timestamps)
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            if supporting_timestamps
            else None
        ),
        **_amount_counts(transactions),
    }
    for field_name, expected_value in expected.items():
        if statistics.get(field_name) != expected_value:
            raise CaseContextIntegrityError(
                f"case_statistics.{field_name} does not match transaction evidence."
            )


def _validate_references(
    bq2: Mapping[str, Any],
    transactions: Mapping[str, Mapping[str, Any]],
) -> None:
    raw_relationships = _require_list(
        bq2.get("relationships"),
        "bq2.relationships",
    )
    relationship_by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw_relationship in enumerate(raw_relationships):
        relationship = _require_mapping(
            raw_relationship,
            f"bq2.relationships[{index}]",
        )
        relationship_id = relationship.get("relationship_id")
        if not isinstance(relationship_id, str) or not relationship_id:
            raise CaseContextIntegrityError("Relationship ID is invalid.")
        if relationship_id in relationship_by_id:
            raise CaseContextIntegrityError(
                f"Duplicate relationship ID: {relationship_id}."
            )
        relationship_by_id[relationship_id] = relationship
        transaction_ids = _require_list(
            relationship.get("transaction_ids"),
            "relationship.transaction_ids",
        )
        if len(transaction_ids) != len(set(transaction_ids)):
            raise CaseContextIntegrityError(
                f"Relationship {relationship_id} repeats a transaction ID."
            )
        if not set(transaction_ids) <= transactions.keys():
            raise CaseContextIntegrityError(
                f"Relationship {relationship_id} has a dangling transaction ID."
            )
        if relationship.get("transaction_count") != len(transaction_ids):
            raise CaseContextIntegrityError(
                f"Relationship {relationship_id} transaction_count is inconsistent."
            )
        direction = relationship.get("direction")
        expected_role = str(direction).upper()
        for transaction_id in transaction_ids:
            transaction = transactions[transaction_id]
            if (
                expected_role not in transaction["edge_roles"]
                or relationship.get("source_account_key")
                != transaction.get("source_account_key")
                or relationship.get("target_account_key")
                != transaction.get("target_account_key")
                or relationship.get("currency") != transaction.get("currency")
            ):
                raise CaseContextIntegrityError(
                    f"Relationship {relationship_id} does not match its transactions."
                )

    raw_candidates = _require_list(bq2.get("candidates"), "bq2.candidates")
    candidates = []
    for index, raw_candidate in enumerate(raw_candidates):
        candidate = _require_mapping(
            raw_candidate,
            f"bq2.candidates[{index}]",
        )
        account_key = candidate.get("account_key")
        transaction_ids = _require_list(
            candidate.get("transaction_ids"),
            "candidate.transaction_ids",
        )
        candidate_relationship_ids = _require_list(
            candidate.get("relationship_ids"),
            "candidate.relationship_ids",
        )
        if not set(transaction_ids) <= transactions.keys():
            raise CaseContextIntegrityError(
                f"Candidate {account_key} has a dangling transaction ID."
            )
        if len(transaction_ids) != len(set(transaction_ids)):
            raise CaseContextIntegrityError(
                f"Candidate {account_key} repeats a transaction ID."
            )
        if len(candidate_relationship_ids) != len(set(candidate_relationship_ids)):
            raise CaseContextIntegrityError(
                f"Candidate {account_key} repeats a relationship ID."
            )
        if not set(candidate_relationship_ids) <= relationship_by_id.keys():
            raise CaseContextIntegrityError(
                f"Candidate {account_key} has a dangling relationship ID."
            )
        if candidate.get("supporting_transaction_count") != len(transaction_ids):
            raise CaseContextIntegrityError(
                f"Candidate {account_key} supporting transaction count is inconsistent."
            )
        upstream_ids = {
            transaction_id
            for transaction_id in transaction_ids
            if (
                "UPSTREAM" in transactions[transaction_id]["edge_roles"]
                and transactions[transaction_id]["source_account_key"]
                == account_key
            )
        }
        downstream_ids = {
            transaction_id
            for transaction_id in transaction_ids
            if (
                "DOWNSTREAM" in transactions[transaction_id]["edge_roles"]
                and transactions[transaction_id]["target_account_key"]
                == account_key
            )
        }
        if upstream_ids | downstream_ids != set(transaction_ids):
            raise CaseContextIntegrityError(
                f"Candidate {account_key} references unrelated transactions."
            )
        expected_direction = (
            "both"
            if upstream_ids and downstream_ids
            else "upstream"
            if upstream_ids
            else "downstream"
        )
        if (
            candidate.get("upstream_supporting_transaction_count")
            != len(upstream_ids)
            or candidate.get("downstream_supporting_transaction_count")
            != len(downstream_ids)
            or candidate.get("relationship_direction") != expected_direction
        ):
            raise CaseContextIntegrityError(
                f"Candidate {account_key} direction counts are inconsistent."
            )
        distinct_relationships = {
            (
                role,
                transactions[transaction_id]["source_account_key"],
                transactions[transaction_id]["target_account_key"],
            )
            for transaction_id in transaction_ids
            for role in ("UPSTREAM", "DOWNSTREAM")
            if (
                (role == "UPSTREAM" and transaction_id in upstream_ids)
                or (role == "DOWNSTREAM" and transaction_id in downstream_ids)
            )
        }
        if candidate.get("distinct_relationship_count") != len(
            distinct_relationships
        ):
            raise CaseContextIntegrityError(
                f"Candidate {account_key} relationship count is inconsistent."
            )
        expected_relationship_ids = {
            relationship_id
            for relationship_id, relationship in relationship_by_id.items()
            if (
                (
                    relationship.get("direction") == "upstream"
                    and relationship.get("source_account_key") == account_key
                )
                or (
                    relationship.get("direction") == "downstream"
                    and relationship.get("target_account_key") == account_key
                )
            )
            and set(relationship["transaction_ids"]).intersection(transaction_ids)
        }
        if set(candidate_relationship_ids) != expected_relationship_ids:
            raise CaseContextIntegrityError(
                f"Candidate {account_key} relationship references are inconsistent."
            )
        if candidate.get("ranking_policy_version") != (
            CANDIDATE_RANKING_POLICY_VERSION
        ):
            raise CaseContextIntegrityError(
                f"Candidate {account_key} ranking policy version is invalid."
            )
        if candidate.get("ranking_policy_semantics") != (
            CANDIDATE_RANKING_POLICY_SEMANTICS
        ):
            raise CaseContextIntegrityError(
                f"Candidate {account_key} ranking semantics are invalid."
            )
        candidates.append(candidate)

    expected_order = sorted(
        candidates,
        key=lambda candidate: (
            -candidate["supporting_transaction_count"],
            float(candidate["closest_gap_minutes"]),
            candidate["account_key"],
        ),
    )
    if list(candidates) != expected_order:
        raise CaseContextIntegrityError(
            "Candidates do not follow the documented ranking order."
        )
    if [candidate.get("candidate_rank") for candidate in candidates] != list(
        range(1, len(candidates) + 1)
    ):
        raise CaseContextIntegrityError("Candidate ranks are not consecutive.")


def _validate_window_and_provenance(context: Mapping[str, Any]) -> None:
    bq2 = context["bq2"]
    comparison = _require_mapping(
        bq2.get("window_comparison"),
        "bq2.window_comparison",
    )
    horizon = _require_non_negative_int(
        comparison.get("queried_horizon_hours"),
        "window_comparison.queried_horizon_hours",
    )
    if horizon == 0:
        raise CaseContextIntegrityError("queried_horizon_hours must be positive.")
    for window in _require_list(
        comparison.get("available_windows"),
        "window_comparison.available_windows",
    ):
        if window.get("window_hours", horizon + 1) > horizon:
            raise CaseContextIntegrityError(
                "Window comparison exceeds the queried horizon."
            )
    unavailable = _require_list(
        comparison.get("unavailable_windows_hours"),
        "window_comparison.unavailable_windows_hours",
    )
    if any(window <= horizon for window in unavailable):
        raise CaseContextIntegrityError(
            "Unavailable windows must exceed the queried horizon."
        )
    if comparison.get("requires_wider_query") != bool(unavailable):
        raise CaseContextIntegrityError(
            "Window comparison completeness flag is inconsistent."
        )
    if comparison.get("recommended_window") is not None:
        raise CaseContextIntegrityError(
            "Window recommendation must remain unavailable without a policy."
        )

    provenance = _require_mapping(context.get("provenance"), "provenance")
    if provenance.get("queried_window_hours") != horizon:
        raise CaseContextIntegrityError(
            "Provenance horizon does not match window comparison."
        )
    if provenance.get("case_context_schema_version") != context.get(
        "schema_version"
    ):
        raise CaseContextIntegrityError(
            "Provenance schema version does not match context."
        )
    if provenance.get("candidate_ranking_policy_version") != (
        CANDIDATE_RANKING_POLICY_VERSION
    ):
        raise CaseContextIntegrityError(
            "Provenance candidate ranking policy is invalid."
        )


def validate_case_context(context: dict[str, Any]) -> None:
    """Validate the complete, unprojected CaseContext contract."""

    _validate_json_value(context)
    if not isinstance(context, dict):
        raise CaseContextIntegrityError("CaseContext must be a dictionary.")
    if set(context) != _FULL_TOP_LEVEL_KEYS:
        raise CaseContextIntegrityError("CaseContext top-level fields are invalid.")
    if context.get("schema_version") != CASE_CONTEXT_SCHEMA_VERSION:
        raise CaseContextIntegrityError("Unsupported CaseContext schema version.")
    forbidden = _find_forbidden_keys(context)
    if forbidden:
        raise CaseContextIntegrityError(
            "Ground-truth fields are forbidden: " + ", ".join(forbidden)
        )

    alert = _require_mapping(context.get("alert"), "alert")
    bq2 = _require_mapping(context.get("bq2"), "bq2")
    transactions = _transaction_map(
        _require_list(bq2.get("transactions"), "bq2.transactions")
    )
    _validate_alert(alert, transactions)
    _validate_bq1(context)
    _validate_statistics(context, transactions)
    _validate_references(bq2, transactions)
    _validate_window_and_provenance(context)

    quality = _require_mapping(context.get("data_quality"), "data_quality")
    if quality.get("bq1_status") != context["bq1"]["status"]:
        raise CaseContextIntegrityError("Data-quality BQ1 status is inconsistent.")
    if quality.get("model_explanations_available") is not False:
        raise CaseContextIntegrityError(
            "Model explanations cannot be marked available."
        )
    expected_bq1_available = context["bq1"]["status"] == "available"
    if quality.get("bq1_metadata_available") is not expected_bq1_available:
        raise CaseContextIntegrityError(
            "Data-quality BQ1 availability is inconsistent."
        )
    statistics = context["bq2"]["case_statistics"]
    for field_name in (
        "missing_amount_count",
        "invalid_amount_count",
        "zero_amount_count",
        "valid_amount_without_currency_count",
    ):
        if quality.get(field_name) != statistics.get(field_name):
            raise CaseContextIntegrityError(
                f"Data-quality {field_name} is inconsistent."
            )
    comparison = context["bq2"]["window_comparison"]
    if quality.get("window_comparison_incomplete") != comparison.get(
        "requires_wider_query"
    ):
        raise CaseContextIntegrityError(
            "Data-quality window completeness is inconsistent."
        )
    if quality.get("candidate_ranking_limitation") != (
        CANDIDATE_RANKING_POLICY_SEMANTICS
    ):
        raise CaseContextIntegrityError(
            "Data-quality candidate ranking limitation is invalid."
        )
    if quality.get("individual_source_evidence_complete") is not True:
        raise CaseContextIntegrityError(
            "Complete CaseContext must preserve all individual source evidence."
        )


def _validate_projection_references(
    bq2: Mapping[str, Any],
    embedded_transactions: Mapping[str, Mapping[str, Any]],
) -> None:
    relationships = _require_list(
        bq2.get("relationships"),
        "bq2.relationships",
    )
    relationship_ids = set()
    for relationship in relationships:
        relationship_id = relationship.get("relationship_id")
        if not isinstance(relationship_id, str) or not relationship_id:
            raise CaseContextIntegrityError("Projected relationship ID is invalid.")
        if relationship_id in relationship_ids:
            raise CaseContextIntegrityError(
                f"Duplicate projected relationship ID: {relationship_id}."
            )
        relationship_ids.add(relationship_id)
        all_ids = set(relationship.get("transaction_ids", []))
        embedded_ids = set(relationship.get("embedded_transaction_ids", []))
        id_only_ids = set(relationship.get("id_only_transaction_ids", []))
        if embedded_ids | id_only_ids != all_ids or embedded_ids & id_only_ids:
            raise CaseContextIntegrityError(
                f"Projected relationship {relationship_id} references are inconsistent."
            )
        if not embedded_ids <= embedded_transactions.keys():
            raise CaseContextIntegrityError(
                f"Projected relationship {relationship_id} has dangling embedded IDs."
            )
        if relationship.get("transaction_details_complete") != (not id_only_ids):
            raise CaseContextIntegrityError(
                f"Projected relationship {relationship_id} completeness is invalid."
            )

    for candidate in _require_list(bq2.get("candidates"), "bq2.candidates"):
        account_key = candidate.get("account_key")
        if not set(candidate.get("relationship_ids", [])) <= relationship_ids:
            raise CaseContextIntegrityError(
                f"Projected candidate {account_key} has a dangling relationship ID."
            )
        all_ids = set(candidate.get("transaction_ids", []))
        embedded_ids = set(candidate.get("embedded_transaction_ids", []))
        id_only_ids = set(candidate.get("id_only_transaction_ids", []))
        if embedded_ids | id_only_ids != all_ids or embedded_ids & id_only_ids:
            raise CaseContextIntegrityError(
                f"Projected candidate {account_key} references are inconsistent."
            )
        if not embedded_ids <= embedded_transactions.keys():
            raise CaseContextIntegrityError(
                f"Projected candidate {account_key} has dangling embedded IDs."
            )
        if candidate.get("transaction_details_complete") != (not id_only_ids):
            raise CaseContextIntegrityError(
                f"Projected candidate {account_key} completeness is invalid."
            )


def validate_llm_input(context: dict[str, Any]) -> None:
    """Validate a bounded CaseContext projection intended for LLM input."""

    _validate_json_value(context)
    if not isinstance(context, dict):
        raise CaseContextIntegrityError("LLM input must be a dictionary.")
    expected_keys = _FULL_TOP_LEVEL_KEYS | {"projection", "evidence_truncation"}
    if set(context) != expected_keys:
        raise CaseContextIntegrityError("LLM input top-level fields are invalid.")
    if context.get("schema_version") != CASE_CONTEXT_SCHEMA_VERSION:
        raise CaseContextIntegrityError("Unsupported CaseContext schema version.")
    forbidden = _find_forbidden_keys(context)
    if forbidden:
        raise CaseContextIntegrityError(
            "Ground-truth fields are forbidden: " + ", ".join(forbidden)
        )

    bq2 = _require_mapping(context.get("bq2"), "bq2")
    transactions = _transaction_map(
        _require_list(bq2.get("transactions"), "bq2.transactions")
    )
    _validate_alert(context["alert"], transactions)
    _validate_bq1(context)
    _validate_projection_references(bq2, transactions)
    _validate_window_and_provenance(context)

    projection = _require_mapping(context.get("projection"), "projection")
    if projection.get("case_statistics_scope") != "complete_fetched_case":
        raise CaseContextIntegrityError(
            "Projected case statistics must retain complete-case scope."
        )
    truncation = _require_mapping(
        context.get("evidence_truncation"),
        "evidence_truncation",
    )
    count_groups = (
        ("transaction", len(transactions)),
        ("candidate", len(bq2["candidates"])),
        ("relationship", len(bq2["relationships"])),
    )
    any_omitted = False
    for name, actual_included in count_groups:
        original = _require_non_negative_int(
            truncation.get(f"original_{name}_count"),
            f"evidence_truncation.original_{name}_count",
        )
        included = _require_non_negative_int(
            truncation.get(f"included_{name}_count"),
            f"evidence_truncation.included_{name}_count",
        )
        omitted = _require_non_negative_int(
            truncation.get(f"omitted_{name}_count"),
            f"evidence_truncation.omitted_{name}_count",
        )
        if included != actual_included or original != included + omitted:
            raise CaseContextIntegrityError(
                f"{name.title()} truncation counts are inconsistent."
            )
        any_omitted = any_omitted or omitted > 0
    if truncation.get("truncated") != any_omitted:
        raise CaseContextIntegrityError("Overall truncation flag is inconsistent.")
    partial_relationships = sum(
        not relationship.get("transaction_details_complete")
        for relationship in bq2["relationships"]
    )
    if truncation.get(
        "relationships_with_partial_transaction_details"
    ) != partial_relationships:
        raise CaseContextIntegrityError(
            "Partial relationship disclosure count is inconsistent."
        )
    quality = _require_mapping(context.get("data_quality"), "data_quality")
    if quality.get("projected_individual_evidence_complete") != (not any_omitted):
        raise CaseContextIntegrityError(
            "Projected evidence completeness flag is inconsistent."
        )


def serialize_case_context(context: dict[str, Any]) -> str:
    """Serialize a complete or projected context as deterministic strict JSON."""

    _validate_json_value(context)
    if "evidence_truncation" in context:
        validate_llm_input(context)
    else:
        validate_case_context(context)
    try:
        return json.dumps(
            context,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise CaseContextSerializationError(
            "CaseContext cannot be serialized as strict JSON."
        ) from error
