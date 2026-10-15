import ast
import inspect

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from src.bq2 import transforms
from src.bq2.transforms import (
    build_display_edges,
    build_nodes,
    build_window_sensitivity,
    calculate_case_metrics,
    normalize_transaction_timestamp,
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

DISPLAY_COLUMNS = [
    "edge_role",
    "source_account_key",
    "target_account_key",
    "source_entity_id",
    "source_entity_name",
    "target_entity_id",
    "target_entity_name",
    "payment_currency",
    "transaction_count",
    "total_amount",
    "closest_gap_minutes",
    "payment_formats",
]


def make_raw_edges() -> pd.DataFrame:
    """Return the characterization fixture captured from the legacy app."""

    rows = [
        (
            "ALERT",
            "txn_alert",
            "2022-09-03 12:00:00",
            "A",
            "B",
            "EA",
            "Alpha",
            "EB",
            "Beta",
            "100.00",
            "USD",
            "WIRE",
            "0",
        ),
        (
            "UPSTREAM",
            "txn_u1",
            "2022-09-03 06:00:00",
            "C",
            "A",
            "EC",
            "Gamma",
            "EA",
            "Alpha",
            "10.00",
            "USD",
            "ACH",
            "360",
        ),
        (
            "UPSTREAM",
            "txn_u2",
            "2022-09-03 05:59:00",
            "C",
            "A",
            "EC",
            "Gamma",
            "EA",
            "Alpha",
            "15.00",
            "USD",
            "WIRE",
            "361",
        ),
        (
            "UPSTREAM",
            "txn_u3",
            "2022-09-03 00:00:00",
            "D",
            "A",
            "ED",
            None,
            "EA",
            "Alpha",
            None,
            "EUR",
            None,
            "720",
        ),
        (
            "UPSTREAM",
            "txn_u4",
            "2022-09-02 18:00:00",
            "C",
            "A",
            "EC",
            "Gamma",
            "EA",
            "Alpha",
            "20.00",
            "EUR",
            "CARD",
            "1080",
        ),
        (
            "DOWNSTREAM",
            "txn_d1",
            "2022-09-03 18:00:00",
            "B",
            "E",
            "EB",
            "Beta",
            "EE",
            "Epsilon",
            "30.00",
            "USD",
            "WIRE",
            "360",
        ),
        (
            "DOWNSTREAM",
            "txn_d2",
            "2022-09-04 12:00:00",
            "B",
            "E",
            "EB",
            "Beta",
            "EE",
            "Epsilon",
            "40.00",
            "USD",
            "WIRE",
            "1440",
        ),
        (
            "DOWNSTREAM",
            "txn_d3",
            "2022-09-04 18:00:00",
            "B",
            "C",
            "EB",
            "Beta",
            "EC",
            "Gamma",
            "50.00",
            "SGD",
            "ACH",
            "1800",
        ),
        (
            "DOWNSTREAM",
            "txn_d4",
            "2022-09-05 12:00:00",
            "B",
            "F",
            "EB",
            "Beta",
            "EF",
            "Phi",
            "60.00",
            "USD",
            "CASH",
            "2880",
        ),
        (
            "DOWNSTREAM",
            "txn_d5",
            "2022-09-05 12:01:00",
            "B",
            "G",
            "EB",
            "Beta",
            "EG",
            "Eta",
            "70.00",
            "USD",
            "CARD",
            "2881",
        ),
    ]
    return pd.DataFrame(rows, columns=RAW_COLUMNS)


def expected_display_edges() -> pd.DataFrame:
    """Return output recorded from app.py at commit 7380f5c."""

    rows = [
        ("UPSTREAM", "C", "A", "EC", "Gamma", "EA", "Alpha", "USD", 2, 25.0, 360, "ACH, WIRE"),
        ("UPSTREAM", "D", "A", "ED", None, "EA", "Alpha", "EUR", 1, 0.0, 720, ""),
        ("UPSTREAM", "C", "A", "EC", "Gamma", "EA", "Alpha", "EUR", 1, 20.0, 1080, "CARD"),
        ("ALERT", "A", "B", "EA", "Alpha", "EB", "Beta", "USD", 1, 100.0, 0, "WIRE"),
        ("DOWNSTREAM", "B", "E", "EB", "Beta", "EE", "Epsilon", "USD", 2, 70.0, 360, "WIRE"),
        ("DOWNSTREAM", "B", "C", "EB", "Beta", "EC", "Gamma", "SGD", 1, 50.0, 1800, "ACH"),
        ("DOWNSTREAM", "B", "F", "EB", "Beta", "EF", "Phi", "USD", 1, 60.0, 2880, "CASH"),
        ("DOWNSTREAM", "B", "G", "EB", "Beta", "EG", "Eta", "USD", 1, 70.0, 2881, "CARD"),
    ]
    return pd.DataFrame(rows, columns=DISPLAY_COLUMNS)


def test_display_edges_match_committed_legacy_output():
    actual = build_display_edges(make_raw_edges()).reset_index(drop=True)

    assert_frame_equal(actual, expected_display_edges(), check_dtype=False)


def test_relationship_aggregation_keeps_currencies_separate():
    display_edges = build_display_edges(make_raw_edges())
    c_to_a = display_edges[
        (display_edges["source_account_key"] == "C")
        & (display_edges["target_account_key"] == "A")
    ]

    assert set(c_to_a["payment_currency"]) == {"USD", "EUR"}
    assert c_to_a.set_index("payment_currency")["total_amount"].to_dict() == {
        "USD": 25.0,
        "EUR": 20.0,
    }


def test_repeated_relationship_counts_and_payment_formats_match_legacy():
    display_edges = build_display_edges(make_raw_edges())
    c_to_a_usd = display_edges[
        (display_edges["source_account_key"] == "C")
        & (display_edges["target_account_key"] == "A")
        & (display_edges["payment_currency"] == "USD")
    ].iloc[0]
    b_to_e = display_edges[
        (display_edges["source_account_key"] == "B")
        & (display_edges["target_account_key"] == "E")
    ].iloc[0]

    assert c_to_a_usd["transaction_count"] == 2
    assert c_to_a_usd["payment_formats"] == "ACH, WIRE"
    assert b_to_e["transaction_count"] == 2
    assert b_to_e["total_amount"] == 70.0


def test_missing_optional_values_keep_legacy_aggregation_behavior():
    display_edges = build_display_edges(make_raw_edges())
    d_to_a = display_edges[
        (display_edges["source_account_key"] == "D")
        & (display_edges["target_account_key"] == "A")
    ].iloc[0]

    assert pd.isna(d_to_a["source_entity_name"])
    assert d_to_a["total_amount"] == 0.0
    assert d_to_a["payment_formats"] == ""


def test_anchor_and_graph_ready_node_roles_remain_identifiable():
    display_edges = build_display_edges(make_raw_edges())
    nodes = build_nodes(display_edges, "A", "B")
    nodes_by_account = {node["account_key"]: node for node in nodes}
    alert_edges = display_edges[display_edges["edge_role"] == "ALERT"]

    assert len(alert_edges) == 1
    assert alert_edges.iloc[0]["source_account_key"] == "A"
    assert alert_edges.iloc[0]["target_account_key"] == "B"
    assert nodes_by_account["A"]["roles"] == {"ALERT_SENDER"}
    assert nodes_by_account["B"]["roles"] == {"ALERT_RECEIVER"}
    assert nodes_by_account["C"]["roles"] == {"UPSTREAM", "DOWNSTREAM"}


def test_case_metrics_use_distinct_account_union_when_roles_overlap():
    display_edges = build_display_edges(make_raw_edges())
    nodes = build_nodes(display_edges, "A", "B")

    assert calculate_case_metrics(nodes, "A", "B") == {
        "connected_accounts": 5,
        "upstream_accounts": 2,
        "downstream_accounts": 4,
    }


@pytest.mark.parametrize(
    ("raw_timestamp", "expected"),
    [
        (" 2022-09-03 12:00:00 ", "2022-09-03 12:00:00"),
        ("2022-09-03 12:00", None),
        ("not-a-date", None),
    ],
)
def test_timestamp_normalization_matches_legacy(raw_timestamp, expected):
    assert normalize_transaction_timestamp(raw_timestamp) == expected


def test_window_sensitivity_matches_committed_legacy_output():
    actual = build_window_sensitivity(make_raw_edges(), "A", "B", 48)
    expected = pd.DataFrame(
        [
            (6, 1, 1, 2, 2),
            (12, 2, 1, 3, 4),
            (24, 2, 1, 3, 6),
            (30, 2, 2, 3, 7),
            (48, 2, 3, 4, 8),
        ],
        columns=[
            "Window (hours)",
            "Upstream accounts",
            "Downstream accounts",
            "Connected accounts",
            "Connected transaction edges",
        ],
    )

    assert_frame_equal(actual, expected)


def test_window_boundaries_are_inclusive_and_horizon_is_not_overstated():
    stats = build_window_sensitivity(make_raw_edges(), "A", "B", 24)

    assert stats["Window (hours)"].tolist() == [6, 12, 24]
    assert stats.set_index("Window (hours)").loc[6].to_dict() == {
        "Upstream accounts": 1,
        "Downstream accounts": 1,
        "Connected accounts": 2,
        "Connected transaction edges": 2,
    }
    assert stats.set_index("Window (hours)").loc[24][
        "Connected transaction edges"
    ] == 6


def test_72_hour_window_is_only_reported_when_query_horizon_allows_it():
    stats = build_window_sensitivity(make_raw_edges(), "A", "B", 72)
    seventy_two_hours = stats.set_index("Window (hours)").loc[72]

    assert stats["Window (hours)"].tolist() == [6, 12, 24, 30, 48, 72]
    assert seventy_two_hours.to_dict() == {
        "Upstream accounts": 2,
        "Downstream accounts": 4,
        "Connected accounts": 5,
        "Connected transaction edges": 9,
    }


def test_empty_direction_and_schema_preserve_legacy_behavior():
    raw_edges = make_raw_edges()
    alert_and_downstream = raw_edges[
        raw_edges["edge_role"].isin(["ALERT", "DOWNSTREAM"])
    ]
    one_direction_stats = build_window_sensitivity(
        alert_and_downstream,
        "A",
        "B",
        6,
    )

    assert one_direction_stats.iloc[0]["Upstream accounts"] == 0
    assert one_direction_stats.iloc[0]["Downstream accounts"] == 1

    empty_raw_edges = raw_edges.iloc[0:0]
    assert build_display_edges(empty_raw_edges).empty
    empty_stats = build_window_sensitivity(empty_raw_edges, "A", "B", 6)
    assert empty_stats.iloc[0].to_dict() == {
        "Window (hours)": 6,
        "Upstream accounts": 0,
        "Downstream accounts": 0,
        "Connected accounts": 0,
        "Connected transaction edges": 0,
    }


def test_empty_node_input_retains_legacy_key_error():
    with pytest.raises(KeyError, match="A"):
        build_nodes(pd.DataFrame(), "A", "B")


def test_transformations_do_not_mutate_input_frames():
    raw_edges = make_raw_edges()
    original_raw_edges = raw_edges.copy(deep=True)

    display_edges = build_display_edges(raw_edges)
    build_window_sensitivity(raw_edges, "A", "B", 48)
    original_display_edges = display_edges.copy(deep=True)
    nodes = build_nodes(display_edges, "A", "B")
    calculate_case_metrics(nodes, "A", "B")

    assert_frame_equal(raw_edges, original_raw_edges)
    assert_frame_equal(display_edges, original_display_edges)


def test_raw_transaction_evidence_remains_available_after_transformation():
    raw_edges = make_raw_edges()
    build_display_edges(raw_edges)

    assert raw_edges["transaction_id"].is_unique
    assert raw_edges.loc[1, "transaction_id"] == "txn_u1"
    assert raw_edges.loc[1, "transaction_timestamp"] == "2022-09-03 06:00:00"


def test_transform_module_is_reusable_without_ui_or_aws_dependencies():
    tree = ast.parse(inspect.getsource(transforms))
    imported_roots = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint(
        {"streamlit", "boto3", "botocore", "pyvis"}
    )
