import ast
import inspect
import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from src.bq1.contracts import BQ1MetadataResult
from src.bq1.service import BQ1MetadataIntegrityError
from src.context import builder, projection, validation
from src.context.builder import build_case_context
from src.context.validation import (
    CaseContextIntegrityError,
    CaseContextSerializationError,
    serialize_case_context,
    validate_case_context,
)
from tests.unit.context_fixtures import (
    ALERT_ID,
    ALERT_TIMESTAMP,
    make_bq1_result,
    make_context,
    make_handoff,
    make_raw_edges,
)


def test_builds_valid_bq1_and_bq2_context():
    context = make_context()

    assert context["schema_version"] == "1.0"
    assert context["alert"]["transaction_id"] == ALERT_ID
    assert context["bq1"]["status"] == "available"
    assert context["bq1"]["metadata"]["risk_score"] == 0.84
    assert context["bq2"]["case_statistics"][
        "total_transactions_including_alert"
    ] == 8
    validate_case_context(context)


def test_bq1_unavailable_preserves_valid_bq2_context():
    context = build_case_context(
        handoff=make_handoff(),
        raw_edges=make_raw_edges(),
        bq1_result=BQ1MetadataResult.unavailable("Offline test."),
        queried_window_hours=48,
    )

    assert context["bq1"]["status"] == "unavailable"
    assert context["bq1"]["metadata"] is None
    assert context["data_quality"]["bq1_metadata_available"] is False
    assert len(context["bq2"]["transactions"]) == 8


def test_bq1_not_found_is_distinct_from_unavailable():
    context = build_case_context(
        handoff=make_handoff(),
        raw_edges=make_raw_edges(),
        bq1_result=BQ1MetadataResult.not_found("No matching score row."),
        queried_window_hours=48,
    )

    assert context["bq1"]["status"] == "not_found"
    assert context["bq1"]["detail"] == "No matching score row."


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("transaction_id", "txn_wrong"),
        ("source_account_key", "WRONG_SOURCE"),
        ("target_account_key", "WRONG_TARGET"),
        (
            "transaction_timestamp",
            ALERT_TIMESTAMP + timedelta(minutes=1),
        ),
    ],
)
def test_bq1_metadata_mismatch_is_rejected(field_name, value):
    with pytest.raises(BQ1MetadataIntegrityError, match="does not match"):
        build_case_context(
            handoff=make_handoff(),
            raw_edges=make_raw_edges(),
            bq1_result=make_bq1_result(**{field_name: value}),
            queried_window_hours=48,
        )


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("transaction_id", "txn_other_alert", "transaction_id"),
        ("source_account_key", "WRONG_SOURCE", "source_account_key"),
        ("target_account_key", "WRONG_TARGET", "target_account_key"),
        (
            "transaction_timestamp",
            "2022-09-03 12:01:00",
            "transaction_timestamp",
        ),
    ],
)
def test_alert_handoff_mismatch_is_rejected(column, value, message):
    raw_edges = make_raw_edges()
    raw_edges.loc[raw_edges["edge_role"] == "ALERT", column] = value

    with pytest.raises(CaseContextIntegrityError, match=message):
        build_case_context(
            handoff=make_handoff(),
            raw_edges=raw_edges,
            bq1_result=make_bq1_result(),
            queried_window_hours=48,
        )


def test_missing_anchor_alert_is_rejected():
    raw_edges = make_raw_edges()
    raw_edges = raw_edges[raw_edges["edge_role"] != "ALERT"]

    with pytest.raises(CaseContextIntegrityError, match="does not contain an ALERT"):
        build_case_context(
            handoff=make_handoff(),
            raw_edges=raw_edges,
            bq1_result=make_bq1_result(),
            queried_window_hours=48,
        )


def test_alert_with_zero_neighbours_is_valid():
    raw_edges = make_raw_edges().iloc[[0]].copy()

    context = build_case_context(
        handoff=make_handoff(),
        raw_edges=raw_edges,
        bq1_result=BQ1MetadataResult.unavailable(),
        queried_window_hours=48,
    )

    assert context["bq2"]["case_statistics"][
        "neighbour_transactions_excluding_alert"
    ] == 0
    assert context["bq2"]["candidates"] == []
    assert context["bq2"]["relationships"]


def test_repeated_transfers_are_preserved_and_counted():
    context = make_context()

    assert context["bq2"]["case_statistics"]["repeated_relationship_count"] == 2
    assert any(
        candidate["repeated_transfer_indicator"]
        for candidate in context["bq2"]["candidates"]
    )


def test_candidate_can_appear_on_both_sides():
    candidates = {
        candidate["account_key"]: candidate
        for candidate in make_context()["bq2"]["candidates"]
    }

    candidate = candidates["SYNTHETIC_UPSTREAM_1"]
    assert candidate["relationship_direction"] == "both"
    assert candidate["upstream_supporting_transaction_count"] == 2
    assert candidate["downstream_supporting_transaction_count"] == 1


def test_multiple_currencies_never_share_a_total():
    totals = make_context()["bq2"]["case_statistics"][
        "amount_totals_by_currency"
    ]

    assert totals == {"EUR": "25.50", "SGD": "60.00", "USD": "156.00"}


def test_missing_invalid_and_true_zero_amounts_remain_distinct():
    statistics = make_context()["bq2"]["case_statistics"]

    assert statistics["missing_amount_count"] == 1
    assert statistics["invalid_amount_count"] == 1
    assert statistics["zero_amount_count"] == 1
    assert statistics["valid_amount_count"] == 6


def test_candidate_order_is_stable_and_follows_documented_policy():
    first = make_context()["bq2"]["candidates"]
    second = make_context()["bq2"]["candidates"]

    assert first == second
    assert [candidate["candidate_rank"] for candidate in first] == [1, 2, 3, 4]
    assert first[0]["account_key"] == "SYNTHETIC_UPSTREAM_1"
    assert first[0]["ranking_policy_semantics"] == (
        "provisional_investigation_order_not_a_validated_risk_score"
    )


def test_candidate_and_relationship_transaction_references_resolve():
    context = make_context()
    transaction_ids = {
        transaction["transaction_id"]
        for transaction in context["bq2"]["transactions"]
    }

    for collection_name in ("candidates", "relationships"):
        for record in context["bq2"][collection_name]:
            assert set(record["transaction_ids"]) <= transaction_ids


def test_candidate_relationship_references_resolve():
    context = make_context()
    relationship_ids = {
        relationship["relationship_id"]
        for relationship in context["bq2"]["relationships"]
    }

    for candidate in context["bq2"]["candidates"]:
        assert set(candidate["relationship_ids"]) <= relationship_ids


def test_window_comparison_never_exceeds_queried_horizon():
    comparison = make_context()["bq2"]["window_comparison"]

    assert max(
        window["window_hours"] for window in comparison["available_windows"]
    ) <= 48
    assert comparison["unavailable_windows_hours"] == [72]
    assert comparison["requires_wider_query"] is True


def test_context_is_strict_json_and_round_trips():
    context = make_context()
    serialized = serialize_case_context(context)

    assert json.loads(serialized) == context
    assert serialize_case_context(context) == serialized


@pytest.mark.parametrize("invalid_value", [float("nan"), float("inf")])
def test_non_finite_values_are_rejected(invalid_value):
    context = make_context()
    context["bq1"]["metadata"]["risk_score"] = invalid_value

    with pytest.raises(CaseContextSerializationError, match="Non-finite"):
        serialize_case_context(context)


@pytest.mark.parametrize(
    "invalid_value",
    [Decimal("1.00"), pd.Timestamp("2022-09-03"), {"not", "json"}],
)
def test_unsupported_runtime_objects_are_rejected(invalid_value):
    context = make_context()
    context["provenance"]["unsupported"] = invalid_value

    with pytest.raises(CaseContextSerializationError, match="Unsupported"):
        serialize_case_context(context)


def test_ground_truth_fields_are_rejected_anywhere():
    context = make_context()
    context["bq1"]["evaluation_is_laundering"] = 1

    with pytest.raises(CaseContextIntegrityError, match="Ground-truth"):
        validate_case_context(context)


def test_evaluation_only_fields_do_not_leak_from_source_objects():
    serialized = serialize_case_context(make_context())

    assert "evaluation_is_laundering" not in serialized
    assert "data_split" not in serialized
    assert "pattern_type" not in serialized


def test_model_explanations_are_explicitly_unavailable():
    context = make_context()

    assert context["bq1"]["model_explanations"] == {
        "status": "unavailable",
        "explanations": None,
        "detail": (
            "No verified feature-attribution evidence is available in the "
            "current BQ1 serving contract."
        ),
    }
    assert context["data_quality"]["model_explanations_available"] is False


def test_context_modules_have_no_aws_streamlit_or_llm_dependencies():
    imported_roots = set()
    for module in (builder, projection, validation):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint(
        {"app", "boto3", "botocore", "openai", "streamlit"}
    )


def test_validator_detects_tampered_case_count():
    context = deepcopy(make_context())
    context["bq2"]["case_statistics"]["upstream_transaction_count"] = 99

    with pytest.raises(CaseContextIntegrityError, match="does not match"):
        validate_case_context(context)


def test_generated_complete_example_passes_contract_validator():
    example_path = Path(
        "docs/handovers/examples/case_context.synthetic.json"
    )
    context = json.loads(example_path.read_text(encoding="utf-8"))

    validate_case_context(context)
