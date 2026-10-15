"""Deterministic bounded projection of a complete CaseContext."""

from copy import deepcopy
from typing import Any

from src.context.validation import validate_case_context, validate_llm_input


def _validate_limit(value: int, field_name: str, *, minimum: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        raise ValueError(f"{field_name} must be an integer >= {minimum}.")


def _annotate_transaction_references(
    record: dict[str, Any],
    embedded_ids: set[str],
) -> dict[str, Any]:
    annotated = deepcopy(record)
    all_ids = record["transaction_ids"]
    annotated["embedded_transaction_ids"] = [
        transaction_id
        for transaction_id in all_ids
        if transaction_id in embedded_ids
    ]
    annotated["id_only_transaction_ids"] = [
        transaction_id
        for transaction_id in all_ids
        if transaction_id not in embedded_ids
    ]
    annotated["transaction_details_complete"] = not annotated[
        "id_only_transaction_ids"
    ]
    return annotated


def build_llm_input(
    context: dict[str, Any],
    *,
    max_candidates: int = 20,
    max_transactions: int = 100,
) -> dict[str, Any]:
    """Return a bounded, reference-safe projection of a full CaseContext."""

    _validate_limit(max_candidates, "max_candidates", minimum=0)
    _validate_limit(max_transactions, "max_transactions", minimum=1)
    validate_case_context(context)

    projected = deepcopy(context)
    full_bq2 = context["bq2"]
    transactions = full_bq2["transactions"]
    candidates = full_bq2["candidates"]
    relationships = full_bq2["relationships"]
    selected_candidates = candidates[:max_candidates]

    alert_id = context["alert"]["transaction_id"]
    transaction_by_id = {
        transaction["transaction_id"]: transaction
        for transaction in transactions
    }
    priority_ids = [alert_id]
    seen_ids = {alert_id}
    for candidate in selected_candidates:
        for transaction_id in candidate["transaction_ids"]:
            if transaction_id not in seen_ids:
                priority_ids.append(transaction_id)
                seen_ids.add(transaction_id)
    for transaction in transactions:
        transaction_id = transaction["transaction_id"]
        if transaction_id not in seen_ids:
            priority_ids.append(transaction_id)
            seen_ids.add(transaction_id)

    if max_transactions >= len(transactions):
        included_ids = [
            transaction["transaction_id"] for transaction in transactions
        ]
    else:
        included_ids = priority_ids[:max_transactions]
    included_id_set = set(included_ids)
    projected_transactions = [
        deepcopy(transaction_by_id[transaction_id])
        for transaction_id in included_ids
    ]

    required_relationship_ids = {
        relationship_id
        for candidate in selected_candidates
        for relationship_id in candidate["relationship_ids"]
    }
    projected_relationships = []
    for relationship in relationships:
        if (
            relationship["relationship_id"] in required_relationship_ids
            or included_id_set.intersection(relationship["transaction_ids"])
        ):
            projected_relationships.append(
                _annotate_transaction_references(
                    relationship,
                    included_id_set,
                )
            )

    projected_candidates = [
        _annotate_transaction_references(candidate, included_id_set)
        for candidate in selected_candidates
    ]
    projected["bq2"]["transactions"] = projected_transactions
    projected["bq2"]["relationships"] = projected_relationships
    projected["bq2"]["candidates"] = projected_candidates

    relationships_with_partial_details = sum(
        not relationship["transaction_details_complete"]
        for relationship in projected_relationships
    )
    count_values = {
        "transaction": (len(transactions), len(projected_transactions)),
        "candidate": (len(candidates), len(projected_candidates)),
        "relationship": (len(relationships), len(projected_relationships)),
    }
    truncation: dict[str, Any] = {}
    for name, (original, included) in count_values.items():
        truncation[f"original_{name}_count"] = original
        truncation[f"included_{name}_count"] = included
        truncation[f"omitted_{name}_count"] = original - included
    truncation["relationships_with_partial_transaction_details"] = (
        relationships_with_partial_details
    )
    truncation["truncated"] = any(
        truncation[f"omitted_{name}_count"] > 0
        for name in count_values
    )

    projected["projection"] = {
        "type": "bounded_llm_input",
        "max_candidates": max_candidates,
        "max_transactions": max_transactions,
        "case_statistics_scope": "complete_fetched_case",
        "individual_evidence_scope": (
            "bounded_projection_with_explicit_id_only_references"
        ),
    }
    projected["evidence_truncation"] = truncation
    projected["data_quality"]["projected_individual_evidence_complete"] = (
        not truncation["truncated"]
    )

    validate_llm_input(projected)
    return projected
