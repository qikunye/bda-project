import ast
import hashlib
import inspect

import pytest

from src.bq2 import queries
from src.bq2.queries import build_alert_lookup_query, build_case_edges_query

TABLE_NAME = "is459_aml_gold.bq2_graph_edges_medium_v1"
TRANSACTION_ID = "txn_630dea1dda9482dbd765a966355d6708"
SENDER = "120075|81294D7A0"
RECEIVER = "15126|8127C1990"
TIMESTAMP = "2022-09-03 12:27:00"

LEGACY_ALERT_SQL_HASH = (
    "ec1dbdf509415c26e7da9ecff83e51e5563a3545095db22339760b7c344e1614"
)
LEGACY_CASE_SQL_HASHES = {
    6: "16289f6f741e03497b58a095ce8297ab7a951821e0dc8fbb046e2c40c5331604",
    24: "b628d4254e126aa9e4eb17f2f79b7db5ab949c0550b2694971e9edcc3616392e",
    48: "3435a78423dddbd6b4567f88f4f1f05312cd27d8d6f486977836f5a318762598",
    72: "e4740f71d69b73468d19374f6f35921c117d53e526197f4e5d809949bef47501",
}


def normalize_sql(sql: str) -> str:
    return " ".join(sql.split())


def sql_hash(sql: str) -> str:
    return hashlib.sha256(normalize_sql(sql).encode()).hexdigest()


def build_case_query(window_hours: int = 48) -> str:
    return build_case_edges_query(
        SENDER,
        RECEIVER,
        TIMESTAMP,
        TRANSACTION_ID,
        window_hours * 60,
        table_name=TABLE_NAME,
    )


def test_alert_lookup_matches_legacy_sql_and_identifiers():
    sql = build_alert_lookup_query("txn_abc123", table_name=TABLE_NAME)

    assert sql_hash(sql) == LEGACY_ALERT_SQL_HASH
    assert f"FROM {TABLE_NAME}" in sql
    assert "WHERE transaction_id = 'txn_abc123'" in sql
    assert "LIMIT 1" in sql


@pytest.mark.parametrize("window_hours", [6, 24, 48, 72])
def test_case_query_matches_legacy_sql_for_supported_windows(window_hours):
    sql = build_case_query(window_hours)

    assert sql_hash(sql) == LEGACY_CASE_SQL_HASHES[window_hours]
    assert f"{window_hours * 60}\n                AS window_minutes" in sql


def test_case_query_preserves_alert_metadata_and_anchor_transaction():
    sql = build_case_query()

    assert f"'{TRANSACTION_ID}'" in sql
    assert f"'{SENDER}'" in sql
    assert f"'{RECEIVER}'" in sql
    assert f"TIMESTAMP '{TIMESTAMP}'" in sql
    assert "'ALERT' AS edge_role" in sql
    assert "g.transaction_id =\n            p.alert_transaction_id" in sql
    assert "g.txn_date =\n            CAST(" in sql
    assert "SELECT *\n    FROM alert_edge" in sql


def test_case_query_preserves_upstream_direction_and_inclusive_window():
    sql = build_case_query()

    assert "'UPSTREAM' AS edge_role" in sql
    assert "g.target_account_key =\n            p.alert_sender" in sql
    assert "-p.window_minutes" in sql
    assert "g.transaction_timestamp\n            <= p.alert_timestamp" in sql
    assert (
        "DATE_DIFF(\n                'minute',\n"
        "                g.transaction_timestamp,\n"
        "                p.alert_timestamp\n            )\n"
        "            <= p.window_minutes"
    ) in sql


def test_case_query_preserves_downstream_direction_and_inclusive_window():
    sql = build_case_query()

    assert "'DOWNSTREAM'\n                AS edge_role" in sql
    assert "g.source_account_key =\n            p.alert_receiver" in sql
    assert "g.transaction_timestamp\n            >= p.alert_timestamp" in sql
    assert (
        "DATE_DIFF(\n                'minute',\n"
        "                p.alert_timestamp,\n"
        "                g.transaction_timestamp\n            )\n"
        "            <= p.window_minutes"
    ) in sql


def test_case_query_preserves_all_date_partition_predicates():
    sql = build_case_query()

    assert sql.count("g.txn_date") == 3
    assert sql.count("g.txn_date BETWEEN") == 2
    assert sql.count("CAST(\n                    p.alert_timestamp\n                    AS DATE") == 2
    assert "DATE_ADD(\n                        'minute',\n                        -p.window_minutes" in sql
    assert "DATE_ADD(\n                        'minute',\n                        p.window_minutes" in sql


@pytest.mark.parametrize(
    "transaction_id",
    ["", "txn_", "bad-id", "txn_abc' OR 1=1 --", None],
)
def test_invalid_transaction_ids_are_rejected(transaction_id):
    with pytest.raises(ValueError, match="Invalid transaction ID format"):
        build_alert_lookup_query(transaction_id, table_name=TABLE_NAME)

    with pytest.raises(ValueError, match="Invalid transaction ID format"):
        build_case_edges_query(
            SENDER,
            RECEIVER,
            TIMESTAMP,
            transaction_id,
            360,
            table_name=TABLE_NAME,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sender", "account' OR '1'='1"),
        ("receiver", "account' OR '1'='1"),
        ("timestamp", "2022-09-03 12:27:00' OR '1'='1"),
    ],
)
def test_unsafe_case_literals_are_rejected(field, value):
    values = {
        "sender": SENDER,
        "receiver": RECEIVER,
        "timestamp": TIMESTAMP,
    }
    values[field] = value

    with pytest.raises(ValueError, match=f"Invalid alert {field}"):
        build_case_edges_query(
            values["sender"],
            values["receiver"],
            values["timestamp"],
            TRANSACTION_ID,
            360,
            table_name=TABLE_NAME,
        )


@pytest.mark.parametrize("window_minutes", [0, -1, 360.0, "360", True])
def test_invalid_windows_are_rejected(window_minutes):
    with pytest.raises(ValueError, match="positive integer"):
        build_case_edges_query(
            SENDER,
            RECEIVER,
            TIMESTAMP,
            TRANSACTION_ID,
            window_minutes,
            table_name=TABLE_NAME,
        )


@pytest.mark.parametrize(
    "table_name",
    ["", "unqualified", "database.table; DROP TABLE other", "db.'table'"],
)
def test_invalid_table_names_are_rejected(table_name):
    with pytest.raises(ValueError, match="Invalid qualified Athena table name"):
        build_alert_lookup_query(TRANSACTION_ID, table_name=table_name)


def test_query_module_has_no_streamlit_aws_or_dataframe_dependencies():
    tree = ast.parse(inspect.getsource(queries))
    imported_roots = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint({"streamlit", "boto3", "botocore", "pandas"})
