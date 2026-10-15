import ast
import inspect
import json
from copy import deepcopy
from datetime import datetime, timezone

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from src.bq1.contracts import BQ1MetadataResult, BQ1RiskMetadata
from src.bq1.service import BQ1MetadataIntegrityError, BQ1MetadataQueryError
from src.bq2.evidence import EvidenceIntegrityError
from src.config import BQ1_SCORE_TABLE, BQ2_EDGE_TABLE
from src.context import service
from src.context.projection import build_llm_input
from src.context.service import CaseContextServiceError, load_case_context
from src.context.validation import (
    CaseContextIntegrityError,
    serialize_case_context,
    validate_case_context,
    validate_llm_input,
)
from src.shared.contracts import AlertHandoff
from tests.unit.context_fixtures import make_raw_edges

ALERT_ID = "txn_SyntheticAlert001"
ALERT_TIMESTAMP = datetime(2022, 9, 3, 12, 0, tzinfo=timezone.utc)
SENDER = "SYNTHETIC_ACCOUNT_A"
RECEIVER = "SYNTHETIC_ACCOUNT_B"


def make_handoff(**overrides) -> AlertHandoff:
    values = {
        "transaction_id": ALERT_ID,
        "source_account_key": SENDER,
        "target_account_key": RECEIVER,
        "transaction_timestamp": ALERT_TIMESTAMP,
    }
    values.update(overrides)
    return AlertHandoff(**values)


def make_athena_edges() -> pd.DataFrame:
    frame = make_raw_edges()
    frame.loc[frame["edge_role"] == "ALERT", "transaction_id"] = ALERT_ID
    frame["gap_minutes"] = frame["gap_minutes"].astype(str)
    return frame


def make_bq1_row(**overrides) -> dict[str, object]:
    row = {
        "transaction_id": ALERT_ID,
        "source_account_key": SENDER,
        "target_account_key": RECEIVER,
        "transaction_timestamp": "2022-09-03 12:00:00",
        "risk_score": "0.84",
        "risk_rank": "12",
        "model_name": "synthetic_xgboost",
        "model_version": "synthetic-v1",
    }
    row.update(overrides)
    return row


def make_bq1_result(**overrides) -> BQ1MetadataResult:
    values = {
        **make_bq1_row(),
        "transaction_timestamp": ALERT_TIMESTAMP,
        "risk_score": 0.84,
        "risk_rank": 12,
        "source_table": BQ1_SCORE_TABLE,
    }
    values.update(overrides)
    return BQ1MetadataResult.available(BQ1RiskMetadata(**values))


class RecordingQueryExecutor:
    def __init__(
        self,
        *,
        bq2_result: pd.DataFrame | object | None = None,
        bq1_result: pd.DataFrame | None = None,
        bq1_error: Exception | None = None,
    ):
        self.bq2_result = (
            make_athena_edges() if bq2_result is None else bq2_result
        )
        self.bq1_result = (
            pd.DataFrame([make_bq1_row()])
            if bq1_result is None
            else bq1_result
        )
        self.bq1_error = bq1_error
        self.queries: list[str] = []

    def __call__(self, *, query: str) -> pd.DataFrame:
        self.queries.append(query)
        if f"FROM {BQ1_SCORE_TABLE}" in query:
            if self.bq1_error is not None:
                raise self.bq1_error
            return self.bq1_result.copy(deep=True)
        if isinstance(self.bq2_result, pd.DataFrame):
            return self.bq2_result.copy(deep=True)
        return self.bq2_result

    @property
    def bq1_queries(self) -> list[str]:
        return [
            query for query in self.queries if f"FROM {BQ1_SCORE_TABLE}" in query
        ]

    @property
    def bq2_queries(self) -> list[str]:
        return [
            query for query in self.queries if f"FROM {BQ2_EDGE_TABLE}" in query
        ]


def test_prefetched_bq2_data_causes_zero_queries():
    executor = RecordingQueryExecutor()

    context = load_case_context(
        handoff=make_handoff(),
        raw_edges=make_athena_edges(),
        query_executor=executor,
    )

    assert executor.queries == []
    assert context["bq1"]["status"] == "unavailable"


def test_prefetched_bq1_metadata_causes_zero_bq1_queries():
    executor = RecordingQueryExecutor()

    context = load_case_context(
        handoff=make_handoff(),
        raw_edges=make_athena_edges(),
        bq1_result=make_bq1_result(),
        query_executor=executor,
    )

    assert executor.bq1_queries == []
    assert context["bq1"]["status"] == "available"


def test_existing_bq1_row_causes_zero_bq1_queries():
    executor = RecordingQueryExecutor()

    context = load_case_context(
        handoff=make_handoff(),
        raw_edges=make_athena_edges(),
        bq1_row=make_bq1_row(),
        query_executor=executor,
    )

    assert executor.bq1_queries == []
    assert context["bq1"]["metadata"]["risk_rank"] == 12


def test_handoff_only_triggers_exactly_one_bq2_query():
    executor = RecordingQueryExecutor()

    context = load_case_context(
        handoff=make_handoff(),
        query_executor=executor,
    )

    assert len(executor.bq2_queries) == 1
    assert executor.bq1_queries == []
    assert context["bq1"]["status"] == "unavailable"


def test_enabled_bq1_lookup_adds_exactly_one_query():
    executor = RecordingQueryExecutor()

    context = load_case_context(
        handoff=make_handoff(),
        query_executor=executor,
        fetch_bq1=True,
    )

    assert len(executor.bq2_queries) == 1
    assert len(executor.bq1_queries) == 1
    assert context["bq1"]["status"] == "available"


def test_prefetched_bq2_with_enabled_bq1_lookup_runs_only_bq1_query():
    executor = RecordingQueryExecutor()

    load_case_context(
        handoff=make_handoff(),
        raw_edges=make_athena_edges(),
        query_executor=executor,
        fetch_bq1=True,
    )

    assert executor.bq2_queries == []
    assert len(executor.bq1_queries) == 1


def test_window_hours_are_converted_to_minutes_in_bq2_query():
    executor = RecordingQueryExecutor()

    load_case_context(
        handoff=make_handoff(),
        query_executor=executor,
        queried_window_hours=48,
    )

    assert "2880\n                AS window_minutes" in executor.bq2_queries[0]


def test_existing_date_partition_pruning_is_preserved():
    executor = RecordingQueryExecutor()

    load_case_context(
        handoff=make_handoff(),
        query_executor=executor,
        fetch_bq1=True,
    )

    bq2_sql = executor.bq2_queries[0]
    bq1_sql = executor.bq1_queries[0]
    assert bq2_sql.count("g.txn_date") == 3
    assert bq2_sql.count("g.txn_date BETWEEN") == 2
    assert "txn_date = DATE '2022-09-03'" in bq1_sql


def test_resulting_complete_context_is_valid():
    context = load_case_context(
        handoff=make_handoff(),
        query_executor=RecordingQueryExecutor(),
        fetch_bq1=True,
    )

    validate_case_context(context)
    assert context["provenance"]["queried_window_hours"] == 48


def test_resulting_bounded_llm_payload_is_valid():
    context = load_case_context(
        handoff=make_handoff(),
        raw_edges=make_athena_edges(),
        bq1_result=make_bq1_result(),
    )

    payload = build_llm_input(context, max_candidates=2, max_transactions=4)

    validate_llm_input(payload)
    assert payload["evidence_truncation"]["truncated"] is True


def test_service_output_is_strictly_json_serializable():
    context = load_case_context(
        handoff=make_handoff(),
        raw_edges=make_athena_edges(),
        bq1_result=make_bq1_result(),
    )

    assert json.loads(serialize_case_context(context)) == context


def test_bq1_not_found_remains_distinct_from_unavailable():
    empty_bq1 = pd.DataFrame(columns=make_bq1_row().keys())
    not_found = load_case_context(
        handoff=make_handoff(),
        raw_edges=make_athena_edges(),
        query_executor=RecordingQueryExecutor(bq1_result=empty_bq1),
        fetch_bq1=True,
    )
    unavailable = load_case_context(
        handoff=make_handoff(),
        raw_edges=make_athena_edges(),
    )

    assert not_found["bq1"]["status"] == "not_found"
    assert unavailable["bq1"]["status"] == "unavailable"


def test_bq1_lookup_failure_propagates_as_typed_error():
    executor = RecordingQueryExecutor(bq1_error=RuntimeError("query failed"))

    with pytest.raises(BQ1MetadataQueryError) as error:
        load_case_context(
            handoff=make_handoff(),
            raw_edges=make_athena_edges(),
            query_executor=executor,
            fetch_bq1=True,
        )

    assert isinstance(error.value.__cause__, RuntimeError)


def test_bq1_alert_mismatch_is_rejected():
    mismatched = make_bq1_result(source_account_key="WRONG_SOURCE")

    with pytest.raises(BQ1MetadataIntegrityError, match="does not match"):
        load_case_context(
            handoff=make_handoff(),
            raw_edges=make_athena_edges(),
            bq1_result=mismatched,
        )


def test_bq2_alert_mismatch_is_rejected():
    raw_edges = make_athena_edges()
    raw_edges.loc[
        raw_edges["edge_role"] == "ALERT",
        "source_account_key",
    ] = "WRONG_SOURCE"

    with pytest.raises(CaseContextIntegrityError, match="source_account_key"):
        load_case_context(
            handoff=make_handoff(),
            raw_edges=raw_edges,
        )


def test_missing_bq2_anchor_is_rejected():
    raw_edges = make_athena_edges()
    raw_edges = raw_edges[raw_edges["edge_role"] != "ALERT"]

    with pytest.raises(CaseContextIntegrityError, match="does not contain an ALERT"):
        load_case_context(
            handoff=make_handoff(),
            raw_edges=raw_edges,
        )


def test_empty_neighbour_case_is_valid():
    raw_edges = make_athena_edges().iloc[[0]].copy()

    context = load_case_context(
        handoff=make_handoff(),
        raw_edges=raw_edges,
    )

    assert context["bq2"]["case_statistics"][
        "neighbour_transactions_excluding_alert"
    ] == 0
    assert context["bq2"]["candidates"] == []


def test_conflicting_duplicate_transaction_is_rejected():
    raw_edges = make_athena_edges()
    conflicting = raw_edges.iloc[[1]].copy()
    conflicting["amount_paid"] = "999.99"
    raw_edges = pd.concat([raw_edges, conflicting], ignore_index=True)

    with pytest.raises(EvidenceIntegrityError, match="Conflicting rows"):
        load_case_context(
            handoff=make_handoff(),
            raw_edges=raw_edges,
        )


def test_window_comparison_stops_at_fetched_horizon():
    context = load_case_context(
        handoff=make_handoff(),
        raw_edges=make_athena_edges(),
        queried_window_hours=48,
    )
    comparison = context["bq2"]["window_comparison"]

    assert max(
        row["window_hours"] for row in comparison["available_windows"]
    ) == 48
    assert comparison["unavailable_windows_hours"] == [72]


def test_identical_inputs_produce_deterministic_output():
    raw_edges = make_athena_edges()
    arguments = {
        "handoff": make_handoff(),
        "raw_edges": raw_edges,
        "bq1_result": make_bq1_result(),
    }

    assert load_case_context(**arguments) == load_case_context(**arguments)


def test_input_dataframe_is_not_mutated():
    raw_edges = make_athena_edges()
    original = raw_edges.copy(deep=True)

    load_case_context(
        handoff=make_handoff(),
        raw_edges=raw_edges,
        bq1_result=make_bq1_result(),
    )

    assert_frame_equal(raw_edges, original)


@pytest.mark.parametrize(
    "arguments",
    [
        {"bq1_result": make_bq1_result(), "bq1_row": make_bq1_row()},
        {"bq1_result": make_bq1_result(), "fetch_bq1": True},
        {"bq1_row": make_bq1_row(), "fetch_bq1": True},
    ],
)
def test_contradictory_bq1_inputs_are_rejected(arguments):
    executor = RecordingQueryExecutor()

    with pytest.raises(CaseContextServiceError, match="only one BQ1 source"):
        load_case_context(
            handoff=make_handoff(),
            query_executor=executor,
            **arguments,
        )

    assert executor.queries == []


def test_invalid_horizon_is_rejected_before_query():
    executor = RecordingQueryExecutor()

    with pytest.raises(CaseContextServiceError, match="positive integer"):
        load_case_context(
            handoff=make_handoff(),
            query_executor=executor,
            queried_window_hours=0,
        )

    assert executor.queries == []


def test_non_dataframe_bq2_query_result_is_rejected():
    executor = RecordingQueryExecutor(bq2_result=[])

    with pytest.raises(CaseContextServiceError, match="Pandas DataFrame"):
        load_case_context(
            handoff=make_handoff(),
            query_executor=executor,
        )


def test_service_has_no_aws_streamlit_or_llm_imports():
    tree = ast.parse(inspect.getsource(service))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint(
        {"app", "boto3", "botocore", "openai", "streamlit"}
    )


def test_service_does_not_mutate_executor_results():
    raw_edges = make_athena_edges()
    original = deepcopy(raw_edges.to_dict(orient="records"))
    executor = RecordingQueryExecutor(bq2_result=raw_edges)

    load_case_context(handoff=make_handoff(), query_executor=executor)

    assert raw_edges.to_dict(orient="records") == original
