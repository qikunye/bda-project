import ast
import inspect
import json

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from src.bq2 import evidence
from src.bq2.evidence import (
    CANDIDATE_RANKING_POLICY_SEMANTICS,
    CANDIDATE_RANKING_POLICY_VERSION,
    EvidenceIntegrityError,
    EvidenceSchemaError,
    build_candidate_evidence,
    build_relationship_evidence,
    build_transaction_evidence,
    build_window_comparison,
    calculate_case_statistics,
    rank_candidate_accounts,
)

RAW_COLUMNS = [
    "edge_role",
    "transaction_id",
    "transaction_timestamp",
    "source_account_key",
    "target_account_key",
    "source_entity_id",
    "source_entity_name",
    "target_entity_id",
    "target_entity_name",
    "amount_paid",
    "payment_currency",
    "payment_format",
    "gap_minutes",
]


def raw_edge(
    role,
    transaction_id,
    timestamp,
    source,
    target,
    amount,
    currency,
    payment_format,
    gap_minutes,
    *,
    source_name=None,
    target_name=None,
):
    return {
        "edge_role": role,
        "transaction_id": transaction_id,
        "transaction_timestamp": timestamp,
        "source_account_key": source,
        "target_account_key": target,
        "source_entity_id": f"entity-{source}",
        "source_entity_name": source_name,
        "target_entity_id": f"entity-{target}",
        "target_entity_name": target_name,
        "amount_paid": amount,
        "payment_currency": currency,
        "payment_format": payment_format,
        "gap_minutes": gap_minutes,
    }


def make_raw_edges(*, include_duplicate=True) -> pd.DataFrame:
    rows = [
        raw_edge(
            "ALERT",
            "txn_alert",
            "2022-09-03 12:00:00",
            "A",
            "B",
            "100.00",
            "USD",
            "WIRE",
            "0",
            source_name="Alpha",
            target_name="Beta",
        ),
        raw_edge(
            "UPSTREAM",
            "txn_u1",
            "2022-09-03 06:00:00",
            "C",
            "A",
            "10.00",
            "USD",
            "ACH",
            "360",
        ),
        raw_edge(
            "UPSTREAM",
            "txn_u2",
            "2022-09-03 05:59:00",
            "C",
            "A",
            "15.00",
            "USD",
            "WIRE",
            "361",
            source_name="Gamma renamed",
        ),
        raw_edge(
            "UPSTREAM",
            "txn_u3",
            "2022-09-03 00:00:00",
            "D",
            "A",
            None,
            "EUR",
            None,
            "720",
        ),
        raw_edge(
            "UPSTREAM",
            "txn_u4",
            "2022-09-02 18:00:00",
            "C",
            "A",
            "20.00",
            "EUR",
            "CARD",
            "1080",
        ),
        raw_edge(
            "DOWNSTREAM",
            "txn_d1",
            "2022-09-03 18:00:00",
            "B",
            "E",
            "30.00",
            "USD",
            "WIRE",
            "360",
        ),
        raw_edge(
            "DOWNSTREAM",
            "txn_d2",
            "2022-09-04 12:00:00",
            "B",
            "E",
            "40.00",
            "USD",
            "WIRE",
            "1440",
        ),
        raw_edge(
            "DOWNSTREAM",
            "txn_d3",
            "2022-09-04 18:00:00",
            "B",
            "C",
            "50.00",
            "SGD",
            "ACH",
            "1800",
        ),
        raw_edge(
            "DOWNSTREAM",
            "txn_d4",
            "2022-09-05 12:00:00",
            "B",
            "F",
            "0.00",
            "USD",
            "CASH",
            "2880",
        ),
        raw_edge(
            "DOWNSTREAM",
            "txn_d5",
            "2022-09-05 12:01:00",
            "B",
            "G",
            "not-an-amount",
            "USD",
            "CARD",
            "2881",
        ),
    ]
    if include_duplicate:
        rows.insert(2, dict(rows[1]))
    return pd.DataFrame(rows, columns=RAW_COLUMNS)


def relationships_by_key(raw_edges=None):
    records = build_relationship_evidence(
        make_raw_edges() if raw_edges is None else raw_edges
    )
    return {
        (
            record["direction"],
            record["source_account_key"],
            record["target_account_key"],
            record["currency"],
        ): record
        for record in records
    }


def candidates_by_account(raw_edges=None):
    records = build_candidate_evidence(
        make_raw_edges() if raw_edges is None else raw_edges,
        "A",
        "B",
    )
    return {record["account_key"]: record for record in records}


def test_case_statistics_count_unique_transactions_and_separate_alert():
    stats = calculate_case_statistics(make_raw_edges(), "A", "B")

    assert stats["total_transactions_including_alert"] == 10
    assert stats["alert_transaction_count"] == 1
    assert stats["neighbour_transactions_excluding_alert"] == 9
    assert stats["upstream_transaction_count"] == 4
    assert stats["downstream_transaction_count"] == 5


def test_case_statistics_count_distinct_accounts_and_repeated_relationships():
    stats = calculate_case_statistics(make_raw_edges(), "A", "B")

    assert stats["upstream_connected_account_count"] == 2
    assert stats["downstream_connected_account_count"] == 4
    assert stats["unique_connected_account_count"] == 5
    assert stats["repeated_relationship_count"] == 2
    assert stats["distinct_payment_format_count"] == 4


def test_case_statistics_preserve_supporting_timestamp_range():
    stats = calculate_case_statistics(make_raw_edges(), "A", "B")

    assert stats["earliest_supporting_transaction_timestamp"] == (
        "2022-09-02T18:00:00Z"
    )
    assert stats["latest_supporting_transaction_timestamp"] == (
        "2022-09-05T12:01:00Z"
    )


def test_case_amount_totals_are_exact_currency_safe_and_quality_aware():
    stats = calculate_case_statistics(make_raw_edges(), "A", "B")

    assert stats["amount_totals_by_currency"] == {
        "EUR": "20.00",
        "SGD": "50.00",
        "USD": "195.00",
    }
    assert stats["valid_amount_count"] == 8
    assert stats["missing_amount_count"] == 1
    assert stats["invalid_amount_count"] == 1
    assert stats["zero_amount_count"] == 1


def test_transaction_evidence_preserves_unique_source_ids_and_amount_statuses():
    records = build_transaction_evidence(make_raw_edges())
    by_id = {record["transaction_id"]: record for record in records}

    assert len(records) == 10
    assert by_id["txn_u1"]["amount"] == "10.00"
    assert by_id["txn_u3"]["amount"] is None
    assert by_id["txn_u3"]["amount_status"] == "missing"
    assert by_id["txn_d4"]["amount"] == "0.00"
    assert by_id["txn_d4"]["amount_status"] == "valid"
    assert by_id["txn_d5"]["amount"] is None
    assert by_id["txn_d5"]["amount_status"] == "invalid"


def test_relationships_link_repeated_transactions_without_duplicate_counting():
    relationships = relationships_by_key()
    c_to_a_usd = relationships[("upstream", "C", "A", "USD")]
    b_to_e = relationships[("downstream", "B", "E", "USD")]

    assert c_to_a_usd["transaction_count"] == 2
    assert c_to_a_usd["transaction_ids"] == ["txn_u2", "txn_u1"]
    assert c_to_a_usd["amount_total"] == "25.00"
    assert c_to_a_usd["payment_formats"] == ["ACH", "WIRE"]
    assert b_to_e["transaction_count"] == 2
    assert b_to_e["amount_total"] == "70.00"


def test_relationships_never_mix_currencies_or_split_on_entity_names():
    relationships = relationships_by_key()

    c_to_a = {
        key[3]: record
        for key, record in relationships.items()
        if key[0:3] == ("upstream", "C", "A")
    }
    assert set(c_to_a) == {"USD", "EUR"}
    assert c_to_a["USD"]["transaction_count"] == 2
    assert c_to_a["EUR"]["transaction_count"] == 1


def test_relationship_amount_completeness_distinguishes_missing_and_invalid():
    relationships = relationships_by_key()
    missing = relationships[("upstream", "D", "A", "EUR")]
    true_zero = relationships[("downstream", "B", "F", "USD")]
    invalid = relationships[("downstream", "B", "G", "USD")]

    assert missing["amount_total"] is None
    assert missing["missing_amount_count"] == 1
    assert missing["amount_total_is_complete"] is False
    assert true_zero["amount_total"] == "0.00"
    assert true_zero["zero_amount_count"] == 1
    assert true_zero["amount_total_is_complete"] is True
    assert invalid["amount_total"] is None
    assert invalid["invalid_amount_count"] == 1
    assert invalid["amount_total_is_complete"] is False


def test_incomplete_relationship_exposes_known_sum_without_calling_it_total():
    raw_edges = make_raw_edges(include_duplicate=False)
    raw_edges.loc[
        raw_edges["transaction_id"] == "txn_u2",
        "amount_paid",
    ] = None
    relationship = relationships_by_key(raw_edges)[
        ("upstream", "C", "A", "USD")
    ]

    assert relationship["known_amount_total"] == "10.00"
    assert relationship["amount_total"] is None
    assert relationship["amount_total_is_complete"] is False
    assert relationship["missing_amount_count"] == 1


def test_relationship_identifiers_are_stable_across_order_and_entity_metadata():
    raw_edges = make_raw_edges()
    shuffled = raw_edges.sample(frac=1, random_state=7).reset_index(drop=True)
    changed_metadata = shuffled.copy()
    changed_metadata.loc[
        changed_metadata["transaction_id"] == "txn_u2",
        "source_entity_name",
    ] = "Another display name"

    original_ids = {
        record["relationship_id"]
        for record in build_relationship_evidence(raw_edges)
    }
    changed_ids = {
        record["relationship_id"]
        for record in build_relationship_evidence(changed_metadata)
    }

    assert changed_ids == original_ids


def test_every_relationship_transaction_id_exists_in_raw_evidence():
    raw_edges = make_raw_edges()
    raw_transaction_ids = set(raw_edges["transaction_id"])

    for relationship in build_relationship_evidence(raw_edges):
        assert set(relationship["transaction_ids"]) <= raw_transaction_ids
        assert relationship["transaction_count"] == len(
            relationship["transaction_ids"]
        )


def test_candidate_accounts_follow_existing_upstream_downstream_rules():
    candidates = candidates_by_account()

    assert set(candidates) == {"C", "D", "E", "F", "G"}
    assert candidates["D"]["relationship_direction"] == "upstream"
    assert candidates["E"]["relationship_direction"] == "downstream"
    assert candidates["C"]["relationship_direction"] == "both"
    assert "A" not in candidates
    assert "B" not in candidates


def test_candidate_evidence_counts_support_and_links_relationships():
    raw_edges = make_raw_edges()
    candidates = candidates_by_account(raw_edges)
    relationship_ids = {
        record["relationship_id"]
        for record in build_relationship_evidence(raw_edges)
    }
    candidate_c = candidates["C"]

    assert candidate_c["supporting_transaction_count"] == 4
    assert candidate_c["upstream_supporting_transaction_count"] == 3
    assert candidate_c["downstream_supporting_transaction_count"] == 1
    assert candidate_c["distinct_relationship_count"] == 2
    assert set(candidate_c["transaction_ids"]) == {
        "txn_u1",
        "txn_u2",
        "txn_u4",
        "txn_d3",
    }
    assert set(candidate_c["relationship_ids"]) <= relationship_ids
    assert candidate_c["repeated_transfer_indicator"] is True


def test_candidate_currency_counts_amounts_formats_and_timestamps():
    candidates = candidates_by_account()
    candidate_c = candidates["C"]

    assert candidate_c["transaction_counts_by_currency"] == {
        "EUR": 1,
        "SGD": 1,
        "USD": 2,
    }
    assert candidate_c["amount_totals_by_currency"] == {
        "EUR": "20.00",
        "SGD": "50.00",
        "USD": "25.00",
    }
    assert candidate_c["payment_formats"] == ["ACH", "CARD", "WIRE"]
    assert candidate_c["closest_gap_minutes"] == 360
    assert candidate_c["earliest_supporting_transaction_timestamp"] == (
        "2022-09-02T18:00:00Z"
    )
    assert candidate_c["latest_supporting_transaction_timestamp"] == (
        "2022-09-04T18:00:00Z"
    )
    assert candidates["D"]["missing_amount_count"] == 1
    assert candidates["F"]["zero_amount_count"] == 1
    assert candidates["G"]["invalid_amount_count"] == 1
    assert candidates["C"]["candidate_rank"] == 1
    assert candidates["E"]["candidate_rank"] == 2


def test_candidate_ranking_uses_support_gap_then_account_key():
    candidates = [
        {
            "account_key": "Y",
            "supporting_transaction_count": 1,
            "closest_gap_minutes": 5,
        },
        {
            "account_key": "Z",
            "supporting_transaction_count": 2,
            "closest_gap_minutes": 20,
        },
        {
            "account_key": "X",
            "supporting_transaction_count": 1,
            "closest_gap_minutes": 5,
        },
    ]

    ranked = rank_candidate_accounts(candidates)

    assert [candidate["account_key"] for candidate in ranked] == ["Z", "X", "Y"]
    assert [candidate["candidate_rank"] for candidate in ranked] == [1, 2, 3]
    assert all(
        candidate["ranking_policy_version"]
        == CANDIDATE_RANKING_POLICY_VERSION
        for candidate in ranked
    )
    assert all(
        candidate["ranking_policy_semantics"]
        == CANDIDATE_RANKING_POLICY_SEMANTICS
        for candidate in ranked
    )
    assert "candidate_rank" not in candidates[0]


def test_candidate_ranking_does_not_use_amounts():
    candidates = [
        {
            "account_key": "A1",
            "supporting_transaction_count": 1,
            "closest_gap_minutes": 10,
            "amount_totals_by_currency": {"USD": "1.00"},
        },
        {
            "account_key": "A2",
            "supporting_transaction_count": 1,
            "closest_gap_minutes": 10,
            "amount_totals_by_currency": {"USD": "999999.00"},
        },
    ]

    ranked = rank_candidate_accounts(candidates)

    assert [candidate["account_key"] for candidate in ranked] == ["A1", "A2"]


def test_every_candidate_transaction_id_exists_in_raw_and_relationship_evidence():
    raw_edges = make_raw_edges()
    raw_transaction_ids = set(raw_edges["transaction_id"])
    relationship_transaction_ids = {
        transaction_id
        for relationship in build_relationship_evidence(raw_edges)
        for transaction_id in relationship["transaction_ids"]
    }

    for candidate in build_candidate_evidence(raw_edges, "A", "B"):
        assert set(candidate["transaction_ids"]) <= raw_transaction_ids
        assert set(candidate["transaction_ids"]) <= relationship_transaction_ids


def test_anchor_accounts_are_excluded_as_candidates_but_edges_are_preserved():
    raw_edges = make_raw_edges()
    extra_edges = pd.DataFrame(
        [
            raw_edge(
                "UPSTREAM",
                "txn_anchor_up",
                "2022-09-03 11:00:00",
                "B",
                "A",
                "5.00",
                "USD",
                "ACH",
                60,
            ),
            raw_edge(
                "DOWNSTREAM",
                "txn_anchor_down",
                "2022-09-03 13:00:00",
                "B",
                "A",
                "6.00",
                "USD",
                "ACH",
                60,
            ),
        ]
    )
    combined = pd.concat([raw_edges, extra_edges], ignore_index=True)

    candidate_ids = {
        candidate["account_key"]
        for candidate in build_candidate_evidence(combined, "A", "B")
    }
    evidence_ids = {
        record["transaction_id"]
        for record in build_transaction_evidence(combined)
    }

    assert "A" not in candidate_ids
    assert "B" not in candidate_ids
    assert {"txn_anchor_up", "txn_anchor_down"} <= evidence_ids


def test_same_minute_supporting_edges_are_retained_without_ordering_claims():
    raw_edges = pd.DataFrame(
        [
            raw_edge(
                "ALERT",
                "txn_alert",
                "2022-09-03 12:00:00",
                "A",
                "B",
                "10.00",
                "USD",
                "WIRE",
                0,
            ),
            raw_edge(
                "UPSTREAM",
                "txn_same_up",
                "2022-09-03 12:00:00",
                "C",
                "A",
                "1.00",
                "USD",
                "ACH",
                0,
            ),
            raw_edge(
                "DOWNSTREAM",
                "txn_same_down",
                "2022-09-03 12:00:00",
                "B",
                "D",
                "2.00",
                "USD",
                "ACH",
                0,
            ),
        ],
        columns=RAW_COLUMNS,
    )

    candidates = candidates_by_account(raw_edges)

    assert candidates["C"]["closest_gap_minutes"] == 0
    assert candidates["D"]["closest_gap_minutes"] == 0
    assert candidates["C"]["earliest_supporting_transaction_timestamp"] == (
        "2022-09-03T12:00:00Z"
    )


def test_window_comparison_reuses_boundaries_and_stays_within_horizon():
    result = build_window_comparison(
        make_raw_edges(include_duplicate=False),
        "A",
        "B",
        48,
    )

    assert result["queried_horizon_hours"] == 48
    assert [
        window["window_hours"] for window in result["available_windows"]
    ] == [6, 12, 24, 30, 48]
    assert result["unavailable_windows_hours"] == [72]
    assert result["requires_wider_query"] is True
    assert result["recommended_window"] is None


def test_window_comparison_calculates_incremental_changes():
    result = build_window_comparison(
        make_raw_edges(include_duplicate=False),
        "A",
        "B",
        48,
    )
    windows = {
        window["window_hours"]: window
        for window in result["available_windows"]
    }

    assert windows[6]["additional_accounts_from_previous_window"] is None
    assert windows[6]["additional_transactions_from_previous_window"] is None
    assert windows[12]["additional_accounts_from_previous_window"] == 1
    assert windows[12]["additional_transactions_from_previous_window"] == 2
    assert windows[24]["additional_accounts_from_previous_window"] == 0
    assert windows[24]["additional_transactions_from_previous_window"] == 2
    assert windows[30]["additional_accounts_from_previous_window"] == 0
    assert windows[30]["additional_transactions_from_previous_window"] == 1
    assert windows[48]["additional_accounts_from_previous_window"] == 1
    assert windows[48]["additional_transactions_from_previous_window"] == 1


def test_window_comparison_reports_72_hours_only_with_72_hour_horizon():
    result = build_window_comparison(
        make_raw_edges(include_duplicate=False),
        "A",
        "B",
        72,
    )

    assert result["unavailable_windows_hours"] == []
    assert result["requires_wider_query"] is False
    assert result["available_windows"][-1] == {
        "window_hours": 72,
        "upstream_account_count": 2,
        "downstream_account_count": 4,
        "unique_candidate_account_count": 5,
        "neighbour_transaction_edge_count": 9,
        "additional_accounts_from_previous_window": 1,
        "additional_transactions_from_previous_window": 1,
    }


def test_empty_upstream_or_downstream_and_empty_case_are_supported():
    raw_edges = make_raw_edges(include_duplicate=False)
    alert_and_downstream = raw_edges[
        raw_edges["edge_role"].isin(["ALERT", "DOWNSTREAM"])
    ]
    stats = calculate_case_statistics(alert_and_downstream, "A", "B")
    candidates = build_candidate_evidence(alert_and_downstream, "A", "B")

    assert stats["upstream_transaction_count"] == 0
    assert all(
        candidate["relationship_direction"] == "downstream"
        for candidate in candidates
    )

    alert_and_upstream = raw_edges[
        raw_edges["edge_role"].isin(["ALERT", "UPSTREAM"])
    ]
    upstream_candidates = build_candidate_evidence(
        alert_and_upstream,
        "A",
        "B",
    )
    assert all(
        candidate["relationship_direction"] == "upstream"
        for candidate in upstream_candidates
    )

    empty = pd.DataFrame(columns=RAW_COLUMNS)
    empty_stats = calculate_case_statistics(empty, "A", "B")
    assert empty_stats["total_transactions_including_alert"] == 0
    assert empty_stats["earliest_supporting_transaction_timestamp"] is None
    assert build_transaction_evidence(empty) == []
    assert build_relationship_evidence(empty) == []
    assert build_candidate_evidence(empty, "A", "B") == []


def test_optional_entity_columns_are_not_required_for_evidence_grouping():
    raw_edges = make_raw_edges().drop(
        columns=[
            "source_entity_id",
            "source_entity_name",
            "target_entity_id",
            "target_entity_name",
        ]
    )

    relationships = build_relationship_evidence(raw_edges)
    transactions = build_transaction_evidence(raw_edges)

    assert relationships
    assert transactions[0]["source_entity_id"] is None


def test_malformed_schema_and_values_are_rejected_explicitly():
    with pytest.raises(EvidenceSchemaError, match="amount_paid"):
        calculate_case_statistics(
            make_raw_edges().drop(columns=["amount_paid"]),
            "A",
            "B",
        )

    invalid_role = make_raw_edges()
    invalid_role.loc[0, "edge_role"] = "SIDEWAYS"
    with pytest.raises(EvidenceIntegrityError, match="Unsupported BQ2 edge role"):
        build_relationship_evidence(invalid_role)

    invalid_gap = make_raw_edges()
    invalid_gap.loc[0, "gap_minutes"] = "-1"
    with pytest.raises(EvidenceIntegrityError, match="gap_minutes"):
        build_candidate_evidence(invalid_gap, "A", "B")

    invalid_timestamp = make_raw_edges()
    invalid_timestamp.loc[0, "transaction_timestamp"] = "not-a-timestamp"
    with pytest.raises(EvidenceIntegrityError, match="transaction_timestamp"):
        build_transaction_evidence(invalid_timestamp)


def test_conflicting_duplicate_transaction_rows_are_rejected():
    raw_edges = make_raw_edges()
    duplicate_indexes = raw_edges.index[raw_edges["transaction_id"] == "txn_u1"]
    raw_edges.loc[duplicate_indexes[-1], "amount_paid"] = "999.00"

    with pytest.raises(EvidenceIntegrityError, match="Conflicting rows"):
        calculate_case_statistics(raw_edges, "A", "B")


def test_evidence_functions_do_not_mutate_input_dataframe():
    raw_edges = make_raw_edges()
    original = raw_edges.copy(deep=True)

    calculate_case_statistics(raw_edges, "A", "B")
    build_transaction_evidence(raw_edges)
    build_relationship_evidence(raw_edges)
    build_candidate_evidence(raw_edges, "A", "B")
    build_window_comparison(raw_edges, "A", "B", 48)

    assert_frame_equal(raw_edges, original)


def test_all_public_outputs_are_json_serializable_without_nan_or_infinity():
    raw_edges = make_raw_edges()
    outputs = [
        calculate_case_statistics(raw_edges, "A", "B"),
        build_transaction_evidence(raw_edges),
        build_relationship_evidence(raw_edges),
        build_candidate_evidence(raw_edges, "A", "B"),
        build_window_comparison(raw_edges, "A", "B", 48),
    ]

    for output in outputs:
        json.dumps(output, allow_nan=False)


def test_evidence_module_has_no_ui_aws_or_llm_dependencies():
    tree = ast.parse(inspect.getsource(evidence))
    imported_modules = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_roots = {
        "streamlit",
        "boto3",
        "botocore",
        "openai",
        "anthropic",
        "langchain",
    }
    assert {
        module_name.split(".")[0]
        for module_name in imported_modules
    }.isdisjoint(forbidden_roots)
    assert "app" not in imported_modules
    assert not any(
        module_name == "src.aws" or module_name.startswith("src.aws.")
        for module_name in imported_modules
    )
