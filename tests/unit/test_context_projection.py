import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.context.projection import build_llm_input
from src.context.validation import CaseContextIntegrityError, validate_llm_input
from tests.unit.context_fixtures import make_context


def test_small_case_with_no_truncation():
    context = make_context()
    projected = build_llm_input(
        context,
        max_candidates=20,
        max_transactions=100,
    )

    assert projected["evidence_truncation"]["truncated"] is False
    assert projected["bq2"]["transactions"] == context["bq2"]["transactions"]
    assert projected["bq2"]["candidates"] != context["bq2"]["candidates"]
    validate_llm_input(projected)


def test_candidate_limit_retains_higher_ranked_candidates_first():
    context = make_context()
    projected = build_llm_input(context, max_candidates=2, max_transactions=100)

    assert [
        candidate["account_key"] for candidate in projected["bq2"]["candidates"]
    ] == [
        candidate["account_key"] for candidate in context["bq2"]["candidates"][:2]
    ]
    assert projected["evidence_truncation"]["omitted_candidate_count"] == 2


def test_transaction_limit_truncates_individual_evidence():
    projected = build_llm_input(
        make_context(),
        max_candidates=4,
        max_transactions=3,
    )

    assert len(projected["bq2"]["transactions"]) == 3
    assert projected["evidence_truncation"]["omitted_transaction_count"] == 5
    assert projected["evidence_truncation"]["truncated"] is True


def test_alert_transaction_is_always_retained_and_first():
    context = make_context()
    projected = build_llm_input(context, max_candidates=0, max_transactions=1)

    assert [
        transaction["transaction_id"]
        for transaction in projected["bq2"]["transactions"]
    ] == [context["alert"]["transaction_id"]]


def test_supporting_transactions_for_higher_ranked_candidate_are_preferred():
    context = make_context()
    first_candidate = context["bq2"]["candidates"][0]
    projected = build_llm_input(context, max_candidates=1, max_transactions=4)
    projected_ids = {
        transaction["transaction_id"]
        for transaction in projected["bq2"]["transactions"]
    }

    assert set(first_candidate["transaction_ids"]) <= projected_ids


def test_projected_embedded_transaction_references_never_dangle():
    projected = build_llm_input(
        make_context(),
        max_candidates=3,
        max_transactions=4,
    )
    transaction_ids = {
        transaction["transaction_id"]
        for transaction in projected["bq2"]["transactions"]
    }

    for collection_name in ("candidates", "relationships"):
        for record in projected["bq2"][collection_name]:
            assert set(record["embedded_transaction_ids"]) <= transaction_ids
            assert set(record["id_only_transaction_ids"]).isdisjoint(
                transaction_ids
            )
            assert set(record["transaction_ids"]) == (
                set(record["embedded_transaction_ids"])
                | set(record["id_only_transaction_ids"])
            )


def test_projected_candidate_relationship_references_never_dangle():
    projected = build_llm_input(
        make_context(),
        max_candidates=2,
        max_transactions=2,
    )
    relationship_ids = {
        relationship["relationship_id"]
        for relationship in projected["bq2"]["relationships"]
    }

    for candidate in projected["bq2"]["candidates"]:
        assert set(candidate["relationship_ids"]) <= relationship_ids


def test_truncation_counts_and_flags_are_exact():
    context = make_context()
    projected = build_llm_input(context, max_candidates=2, max_transactions=3)
    truncation = projected["evidence_truncation"]

    assert truncation["original_transaction_count"] == 8
    assert truncation["included_transaction_count"] == 3
    assert truncation["omitted_transaction_count"] == 5
    assert truncation["original_candidate_count"] == 4
    assert truncation["included_candidate_count"] == 2
    assert truncation["omitted_candidate_count"] == 2
    assert truncation["truncated"] is True


def test_partial_relationship_details_are_disclosed():
    projected = build_llm_input(
        make_context(),
        max_candidates=1,
        max_transactions=2,
    )
    partial = [
        relationship
        for relationship in projected["bq2"]["relationships"]
        if not relationship["transaction_details_complete"]
    ]

    assert partial
    assert projected["evidence_truncation"][
        "relationships_with_partial_transaction_details"
    ] == len(partial)


def test_full_case_metrics_are_unchanged_by_projection():
    context = make_context()
    projected = build_llm_input(context, max_candidates=1, max_transactions=2)

    assert projected["bq2"]["case_statistics"] == context["bq2"][
        "case_statistics"
    ]
    assert projected["projection"]["case_statistics_scope"] == (
        "complete_fetched_case"
    )


def test_projection_does_not_mutate_original_context():
    context = make_context()
    original = deepcopy(context)

    build_llm_input(context, max_candidates=1, max_transactions=2)

    assert context == original


def test_projection_is_deterministic_for_identical_input():
    context = make_context()

    first = build_llm_input(context, max_candidates=2, max_transactions=4)
    second = build_llm_input(context, max_candidates=2, max_transactions=4)

    assert first == second


def test_projection_retains_bq1_quality_provenance_and_windows():
    context = make_context()
    projected = build_llm_input(context, max_candidates=1, max_transactions=2)

    assert projected["alert"] == context["alert"]
    assert projected["bq1"] == context["bq1"]
    assert projected["bq2"]["window_comparison"] == context["bq2"][
        "window_comparison"
    ]
    assert projected["provenance"] == context["provenance"]
    assert projected["data_quality"]["bq1_status"] == "available"


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("max_candidates", -1, "max_candidates"),
        ("max_candidates", True, "max_candidates"),
        ("max_transactions", 0, "max_transactions"),
        ("max_transactions", 1.5, "max_transactions"),
    ],
)
def test_invalid_projection_limits_are_rejected(field_name, value, message):
    kwargs = {"max_candidates": 2, "max_transactions": 4, field_name: value}

    with pytest.raises(ValueError, match=message):
        build_llm_input(make_context(), **kwargs)


def test_validator_rejects_incorrect_truncation_counts():
    projected = build_llm_input(
        make_context(),
        max_candidates=2,
        max_transactions=3,
    )
    projected["evidence_truncation"]["omitted_transaction_count"] = 999

    with pytest.raises(CaseContextIntegrityError, match="counts are inconsistent"):
        validate_llm_input(projected)


def test_generated_projected_example_passes_contract_validator():
    example_path = Path(
        "docs/handovers/examples/llm_input.synthetic.json"
    )
    projected = json.loads(example_path.read_text(encoding="utf-8"))

    validate_llm_input(projected)
