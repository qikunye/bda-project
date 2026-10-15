"""Pure assembly of BQ1 metadata and BQ2 evidence into a CaseContext."""

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.bq1.contracts import BQ1MetadataResult, BQ1MetadataStatus
from src.bq1.service import validate_metadata_matches_handoff
from src.bq2.evidence import (
    CANDIDATE_RANKING_POLICY_SEMANTICS,
    CANDIDATE_RANKING_POLICY_VERSION,
    build_candidate_evidence,
    build_relationship_evidence,
    build_transaction_evidence,
    build_window_comparison,
    calculate_case_statistics,
)
from src.config import BQ1_SCORE_TABLE, BQ2_EDGE_TABLE
from src.context.validation import (
    CaseContextIntegrityError,
    validate_case_context,
)
from src.shared.contracts import AlertHandoff

CASE_CONTEXT_SCHEMA_VERSION = "1.0"


def _normalize_handoff_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("handoff.transaction_timestamp must be a datetime.")

    if value.tzinfo is None:
        normalized = value.replace(tzinfo=timezone.utc)
    else:
        normalized = value.astimezone(timezone.utc)

    if normalized.second != 0 or normalized.microsecond != 0:
        raise CaseContextIntegrityError(
            "Alert timestamps must follow the source's minute-resolution convention."
        )
    return normalized.isoformat().replace("+00:00", "Z")


def _find_anchor_transaction(
    transactions: list[dict[str, object]],
) -> dict[str, object]:
    anchors = [
        transaction
        for transaction in transactions
        if "ALERT" in transaction["edge_roles"]
    ]
    if not anchors:
        raise CaseContextIntegrityError(
            "BQ2 evidence does not contain an ALERT anchor transaction."
        )
    if len(anchors) != 1:
        raise CaseContextIntegrityError(
            "BQ2 evidence must contain exactly one distinct ALERT transaction."
        )
    return anchors[0]


def _validate_anchor_matches_handoff(
    anchor: dict[str, object],
    handoff: AlertHandoff,
    normalized_timestamp: str,
) -> None:
    checks = (
        ("transaction_id", anchor["transaction_id"], handoff.transaction_id),
        (
            "source_account_key",
            anchor["source_account_key"],
            handoff.source_account_key,
        ),
        (
            "target_account_key",
            anchor["target_account_key"],
            handoff.target_account_key,
        ),
        (
            "transaction_timestamp",
            anchor["transaction_timestamp"],
            normalized_timestamp,
        ),
    )
    for field_name, actual, expected in checks:
        if actual != expected:
            raise CaseContextIntegrityError(
                f"BQ2 ALERT {field_name} does not match the selected handoff."
            )


def _build_bq1_section(
    result: BQ1MetadataResult,
    handoff: AlertHandoff,
) -> dict[str, object]:
    if not isinstance(result, BQ1MetadataResult):
        raise TypeError("bq1_result must be a BQ1MetadataResult.")
    if result.status is BQ1MetadataStatus.AVAILABLE:
        if result.metadata is None:  # Defensive against invalid deserialization.
            raise CaseContextIntegrityError(
                "Available BQ1 metadata result has no metadata."
            )
        validate_metadata_matches_handoff(result.metadata, handoff)

    section = result.to_dict()
    section["model_explanations"] = {
        "status": "unavailable",
        "explanations": None,
        "detail": (
            "No verified feature-attribution evidence is available in the "
            "current BQ1 serving contract."
        ),
    }
    return section


def build_case_context(
    *,
    handoff: AlertHandoff,
    raw_edges: pd.DataFrame,
    bq1_result: BQ1MetadataResult,
    queried_window_hours: int,
) -> dict[str, Any]:
    """Assemble and validate a complete CaseContext from retrieved evidence.

    The function performs no queries and preserves the complete deterministic
    BQ2 evidence generated from ``raw_edges``.
    """

    if not isinstance(handoff, AlertHandoff):
        raise TypeError("handoff must be an AlertHandoff.")

    normalized_timestamp = _normalize_handoff_timestamp(
        handoff.transaction_timestamp
    )
    transactions = build_transaction_evidence(raw_edges)
    anchor = _find_anchor_transaction(transactions)
    _validate_anchor_matches_handoff(anchor, handoff, normalized_timestamp)

    bq1 = _build_bq1_section(bq1_result, handoff)
    case_statistics = calculate_case_statistics(
        raw_edges,
        handoff.source_account_key,
        handoff.target_account_key,
    )
    relationships = build_relationship_evidence(raw_edges)
    candidates = build_candidate_evidence(
        raw_edges,
        handoff.source_account_key,
        handoff.target_account_key,
    )
    window_comparison = build_window_comparison(
        raw_edges,
        handoff.source_account_key,
        handoff.target_account_key,
        queried_window_hours,
    )

    metadata = bq1_result.metadata
    context: dict[str, Any] = {
        "schema_version": CASE_CONTEXT_SCHEMA_VERSION,
        "alert": {
            "transaction_id": handoff.transaction_id,
            "source_account_key": handoff.source_account_key,
            "target_account_key": handoff.target_account_key,
            "transaction_timestamp": normalized_timestamp,
            "amount": anchor["amount"],
            "amount_status": anchor["amount_status"],
            "currency": anchor["currency"],
            "payment_format": anchor["payment_format"],
        },
        "bq1": bq1,
        "bq2": {
            "case_statistics": case_statistics,
            "transactions": transactions,
            "relationships": relationships,
            "candidates": candidates,
            "window_comparison": window_comparison,
        },
        "data_quality": {
            "bq1_status": bq1_result.status.value,
            "bq1_metadata_available": (
                bq1_result.status is BQ1MetadataStatus.AVAILABLE
            ),
            "model_explanations_available": False,
            "missing_amount_count": case_statistics["missing_amount_count"],
            "invalid_amount_count": case_statistics["invalid_amount_count"],
            "zero_amount_count": case_statistics["zero_amount_count"],
            "valid_amount_without_currency_count": case_statistics[
                "valid_amount_without_currency_count"
            ],
            "window_comparison_incomplete": window_comparison[
                "requires_wider_query"
            ],
            "unavailable_window_hours": window_comparison[
                "unavailable_windows_hours"
            ],
            "individual_source_evidence_complete": True,
            "candidate_ranking_limitation": (
                CANDIDATE_RANKING_POLICY_SEMANTICS
            ),
        },
        "provenance": {
            "case_context_schema_version": CASE_CONTEXT_SCHEMA_VERSION,
            "bq1_gold_source_table": (
                metadata.source_table if metadata is not None else BQ1_SCORE_TABLE
            ),
            "bq2_gold_source_table": BQ2_EDGE_TABLE,
            "bq1_model_version": (
                metadata.model_version if metadata is not None else None
            ),
            "queried_window_hours": queried_window_hours,
            "candidate_ranking_policy_version": (
                CANDIDATE_RANKING_POLICY_VERSION
            ),
        },
    }

    validate_case_context(context)
    return context
