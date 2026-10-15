import ast
import inspect
import json
from dataclasses import fields
from datetime import datetime, timezone

import pandas as pd
import pytest

from src.bq1 import contracts, queries, service
from src.bq1.contracts import (
    RISK_RANK_SCOPE,
    RISK_SCORE_SEMANTICS,
    BQ1MetadataStatus,
)
from src.bq1.queries import build_bq1_score_lookup_query
from src.bq1.service import (
    BQ1MetadataIntegrityError,
    BQ1MetadataQueryError,
    build_bq1_metadata_from_row,
    lookup_bq1_metadata,
    unavailable_bq1_metadata,
)
from src.config import BQ1_SCORE_TABLE
from src.shared.contracts import AlertHandoff

TRANSACTION_ID = "txn_630dea1dda9482dbd765a966355d6708"
SENDER = "120075|81294D7A0"
RECEIVER = "15126|8127C1990"
TIMESTAMP = datetime(2022, 9, 3, 12, 27, 0, tzinfo=timezone.utc)

PERMITTED_QUERY_COLUMNS = [
    "transaction_id",
    "source_account_key",
    "target_account_key",
    "transaction_timestamp",
    "risk_score",
    "risk_rank",
    "model_name",
    "model_version",
]


def make_handoff() -> AlertHandoff:
    return AlertHandoff(
        transaction_id=TRANSACTION_ID,
        source_account_key=SENDER,
        target_account_key=RECEIVER,
        transaction_timestamp=TIMESTAMP,
    )


def make_bq1_row(**overrides) -> dict[str, object]:
    row = {
        "transaction_id": TRANSACTION_ID,
        "source_account_key": SENDER,
        "target_account_key": RECEIVER,
        "transaction_timestamp": "2022-09-03 12:27:00.000",
        "risk_score": "0.8125",
        "risk_rank": "6142",
        "model_name": "xgboost",
        "model_version": "v1",
    }
    row.update(overrides)
    return row


class FakeQueryExecutor:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.queries = []

    def __call__(self, *, query: str):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.result


def selected_columns(sql: str) -> list[str]:
    select_clause = sql.split("SELECT", maxsplit=1)[1].split("FROM", maxsplit=1)[0]
    return [
        line.strip().rstrip(",")
        for line in select_clause.splitlines()
        if line.strip()
    ]


def test_score_lookup_uses_canonical_gold_table_and_permitted_columns_only():
    sql = build_bq1_score_lookup_query(
        TRANSACTION_ID,
        "2022-09-03 12:27:00",
        table_name=BQ1_SCORE_TABLE,
    )

    assert f"FROM {BQ1_SCORE_TABLE}" in sql
    assert selected_columns(sql) == PERMITTED_QUERY_COLUMNS
    assert "evaluation_is_laundering" not in sql
    assert "data_split" not in sql


def test_score_lookup_filters_transaction_and_date_partition():
    sql = build_bq1_score_lookup_query(
        TRANSACTION_ID,
        "2022-09-03 23:59:00",
        table_name=BQ1_SCORE_TABLE,
    )

    assert f"WHERE transaction_id = '{TRANSACTION_ID}'" in sql
    assert "AND txn_date = DATE '2022-09-03'" in sql
    assert "LIMIT" not in sql


@pytest.mark.parametrize(
    "transaction_id",
    ["", "txn_", "bad-id", "txn_x' OR 1=1 --", None],
)
def test_score_lookup_rejects_invalid_transaction_ids(transaction_id):
    with pytest.raises(ValueError, match="Invalid transaction ID format"):
        build_bq1_score_lookup_query(
            transaction_id,
            "2022-09-03 12:27:00",
            table_name=BQ1_SCORE_TABLE,
        )


@pytest.mark.parametrize(
    "transaction_timestamp",
    [
        "",
        "2022-09-03",
        "2022-02-30 12:27:00",
        "2022-09-03 12:27:00' OR '1'='1",
        None,
    ],
)
def test_score_lookup_rejects_invalid_timestamps(transaction_timestamp):
    with pytest.raises((TypeError, ValueError), match="Invalid transaction timestamp"):
        build_bq1_score_lookup_query(
            TRANSACTION_ID,
            transaction_timestamp,
            table_name=BQ1_SCORE_TABLE,
        )


@pytest.mark.parametrize(
    "table_name",
    ["", "unqualified", "db.table; DROP TABLE other", "db.'table'"],
)
def test_score_lookup_rejects_invalid_table_names(table_name):
    with pytest.raises(ValueError, match="Invalid qualified Athena table name"):
        build_bq1_score_lookup_query(
            TRANSACTION_ID,
            "2022-09-03 12:27:00",
            table_name=table_name,
        )


def test_existing_bq1_mapping_is_parsed_without_query_execution():
    metadata = build_bq1_metadata_from_row(
        make_bq1_row(),
        handoff=make_handoff(),
    )

    assert metadata.transaction_id == TRANSACTION_ID
    assert metadata.transaction_timestamp == TIMESTAMP
    assert metadata.risk_score == 0.8125
    assert isinstance(metadata.risk_score, float)
    assert metadata.risk_rank == 6142
    assert isinstance(metadata.risk_rank, int)
    assert metadata.model_name == "xgboost"
    assert metadata.model_version == "v1"
    assert metadata.source_table == BQ1_SCORE_TABLE


def test_existing_pandas_series_is_supported():
    metadata = build_bq1_metadata_from_row(pd.Series(make_bq1_row()))

    assert metadata.transaction_id == TRANSACTION_ID
    assert metadata.risk_rank == 6142


def test_null_optional_risk_and_model_metadata_is_explicit():
    metadata = build_bq1_metadata_from_row(
        make_bq1_row(
            risk_score=None,
            risk_rank=None,
            model_name=None,
            model_version=pd.NA,
        )
    )

    assert metadata.risk_score is None
    assert metadata.risk_rank is None
    assert metadata.model_name is None
    assert metadata.model_version is None


def test_ground_truth_and_data_split_never_enter_metadata_contract():
    row = make_bq1_row(
        evaluation_is_laundering="1",
        data_split="test",
    )

    serialized = build_bq1_metadata_from_row(row).to_dict()

    assert "evaluation_is_laundering" not in serialized
    assert "data_split" not in serialized


def test_metadata_is_json_serializable_and_preserves_documented_semantics():
    metadata = build_bq1_metadata_from_row(make_bq1_row())

    serialized = metadata.to_dict()
    assert json.loads(json.dumps(serialized)) == serialized
    assert serialized["risk_score_semantics"] == RISK_SCORE_SEMANTICS
    assert serialized["risk_score_semantics"] == (
        "ranking_signal_not_calibrated_probability"
    )
    assert serialized["risk_rank_scope"] == RISK_RANK_SCOPE
    assert serialized["risk_rank_scope"] == "global_all_scored_rows"


def test_four_field_handoff_drives_partition_pruned_metadata_lookup():
    executor = FakeQueryExecutor(pd.DataFrame([make_bq1_row()]))

    result = lookup_bq1_metadata(
        make_handoff(),
        query_executor=executor,
    )

    assert result.status is BQ1MetadataStatus.AVAILABLE
    assert result.metadata is not None
    assert result.metadata.risk_score == 0.8125
    assert result.metadata.risk_rank == 6142
    assert len(executor.queries) == 1
    assert f"FROM {BQ1_SCORE_TABLE}" in executor.queries[0]
    assert "txn_date = DATE '2022-09-03'" in executor.queries[0]


def test_missing_bq1_record_returns_explicit_not_found_status():
    executor = FakeQueryExecutor(pd.DataFrame(columns=PERMITTED_QUERY_COLUMNS))

    result = lookup_bq1_metadata(
        make_handoff(),
        query_executor=executor,
    )

    assert result.status is BQ1MetadataStatus.NOT_FOUND
    assert result.metadata is None
    assert result.detail == "No BQ1 metadata row matched the selected alert."


def test_bq1_can_be_unavailable_while_four_field_handoff_remains_valid():
    handoff = make_handoff()

    result = unavailable_bq1_metadata("BQ1 lookup was not requested.")

    assert handoff.transaction_id == TRANSACTION_ID
    assert result.to_dict() == {
        "status": "unavailable",
        "metadata": None,
        "detail": "BQ1 lookup was not requested.",
    }


def test_query_failure_is_distinct_from_missing_record():
    executor = FakeQueryExecutor(error=RuntimeError("Athena unavailable"))

    with pytest.raises(BQ1MetadataQueryError, match="BQ1 metadata query failed") as error:
        lookup_bq1_metadata(
            make_handoff(),
            query_executor=executor,
        )

    assert isinstance(error.value.__cause__, RuntimeError)


@pytest.mark.parametrize(
    ("field_name", "inconsistent_value"),
    [
        ("transaction_id", "txn_different"),
        ("source_account_key", "different-sender"),
        ("target_account_key", "different-receiver"),
        ("transaction_timestamp", "2022-09-03 12:28:00"),
    ],
)
def test_inconsistent_bq1_metadata_is_rejected(field_name, inconsistent_value):
    executor = FakeQueryExecutor(
        pd.DataFrame([make_bq1_row(**{field_name: inconsistent_value})])
    )

    with pytest.raises(
        BQ1MetadataIntegrityError,
        match=f"{field_name} does not match",
    ):
        lookup_bq1_metadata(
            make_handoff(),
            query_executor=executor,
        )


def test_duplicate_matching_rows_are_rejected():
    row = make_bq1_row()
    executor = FakeQueryExecutor(pd.DataFrame([row, row]))

    with pytest.raises(BQ1MetadataIntegrityError, match="multiple matching rows"):
        lookup_bq1_metadata(
            make_handoff(),
            query_executor=executor,
        )


def test_invalid_executor_result_is_an_integrity_error():
    executor = FakeQueryExecutor([make_bq1_row()])

    with pytest.raises(BQ1MetadataIntegrityError, match="Pandas DataFrame"):
        lookup_bq1_metadata(
            make_handoff(),
            query_executor=executor,
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("risk_score", "not-numeric"),
        ("risk_score", float("inf")),
        ("risk_rank", "1.5"),
        ("risk_rank", "0"),
    ],
)
def test_invalid_numeric_metadata_is_rejected(field_name, invalid_value):
    with pytest.raises(BQ1MetadataIntegrityError, match=field_name):
        build_bq1_metadata_from_row(
            make_bq1_row(**{field_name: invalid_value})
        )


def test_existing_alert_handoff_remains_exactly_four_fields():
    assert [field.name for field in fields(AlertHandoff)] == [
        "transaction_id",
        "source_account_key",
        "target_account_key",
        "transaction_timestamp",
    ]


def test_bq1_modules_have_no_streamlit_boto_or_aws_executor_dependencies():
    imported_modules = set()

    for module in (contracts, queries, service):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

    assert imported_modules.isdisjoint(
        {"streamlit", "boto3", "botocore", "src.aws"}
    )
    assert not any(
        module_name.startswith(
            ("streamlit.", "boto3.", "botocore.", "src.aws.")
        )
        for module_name in imported_modules
    )
