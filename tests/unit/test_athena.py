from collections.abc import Iterable

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from src.aws.athena import AthenaQueryError, execute_query

QUERY_ID = "query-123"


def athena_row(*values: str | None) -> dict:
    return {
        "Data": [
            {} if value is None else {"VarCharValue": value}
            for value in values
        ]
    }


def result_page(
    columns: tuple[str, ...],
    rows: Iterable[dict],
) -> dict:
    return {
        "ResultSet": {
            "ResultSetMetadata": {
                "ColumnInfo": [
                    {"Name": column}
                    for column in columns
                ]
            },
            "Rows": list(rows),
        }
    }


class FakePaginator:
    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.pages)


class FakeAthenaClient:
    def __init__(self, states: list[dict], pages: list[dict] | None = None):
        self.states = iter(states)
        self.paginator = FakePaginator(pages or [])
        self.start_calls = []
        self.status_calls = []
        self.paginator_names = []

    def start_query_execution(self, **kwargs):
        self.start_calls.append(kwargs)
        return {"QueryExecutionId": QUERY_ID}

    def get_query_execution(self, **kwargs):
        self.status_calls.append(kwargs)
        return {
            "QueryExecution": {
                "Status": next(self.states),
            }
        }

    def get_paginator(self, name: str):
        self.paginator_names.append(name)
        return self.paginator


def execute(client: FakeAthenaClient) -> pd.DataFrame:
    return execute_query(
        client,
        query="SELECT value FROM example",
        database="analytics",
        output_location="s3://example/results/",
        poll_interval_seconds=0,
    )


def test_successful_query_polls_and_returns_all_paginated_rows():
    columns = ("transaction_id", "amount")
    client = FakeAthenaClient(
        states=[
            {"State": "QUEUED"},
            {"State": "RUNNING"},
            {"State": "SUCCEEDED"},
        ],
        pages=[
            result_page(
                columns,
                [
                    athena_row(*columns),
                    athena_row("txn-1", "10.50"),
                ],
            ),
            result_page(
                columns,
                [
                    athena_row("txn-2"),
                ],
            ),
        ],
    )

    actual = execute(client)

    expected = pd.DataFrame(
        [
            ["txn-1", "10.50"],
            ["txn-2", None],
        ],
        columns=columns,
    )
    assert_frame_equal(actual, expected)
    assert client.start_calls == [
        {
            "QueryString": "SELECT value FROM example",
            "QueryExecutionContext": {"Database": "analytics"},
            "ResultConfiguration": {"OutputLocation": "s3://example/results/"},
        }
    ]
    assert client.status_calls == [
        {"QueryExecutionId": QUERY_ID},
        {"QueryExecutionId": QUERY_ID},
        {"QueryExecutionId": QUERY_ID},
    ]
    assert client.paginator_names == ["get_query_results"]
    assert client.paginator.calls == [{"QueryExecutionId": QUERY_ID}]


def test_header_only_result_returns_empty_frame_with_columns():
    columns = ("transaction_id", "amount")
    client = FakeAthenaClient(
        states=[{"State": "SUCCEEDED"}],
        pages=[
            result_page(columns, [athena_row(*columns)]),
        ],
    )

    actual = execute(client)

    expected = pd.DataFrame(columns=columns)
    assert_frame_equal(actual, expected)


def test_failed_query_raises_clean_error_with_reason():
    client = FakeAthenaClient(
        states=[
            {
                "State": "FAILED",
                "StateChangeReason": "Invalid table reference",
            }
        ]
    )

    with pytest.raises(AthenaQueryError) as error:
        execute(client)

    assert str(error.value) == (
        "Athena query query-123 FAILED: Invalid table reference"
    )
    assert client.paginator_names == []


def test_cancelled_query_raises_clean_error():
    client = FakeAthenaClient(
        states=[{"State": "CANCELLED"}],
    )

    with pytest.raises(AthenaQueryError) as error:
        execute(client)

    assert str(error.value) == (
        "Athena query query-123 CANCELLED: Unknown Athena error"
    )
    assert client.paginator_names == []


def test_pagination_skips_header_once_and_includes_each_page_once():
    columns = ("value",)
    client = FakeAthenaClient(
        states=[{"State": "SUCCEEDED"}],
        pages=[
            result_page(
                columns,
                [athena_row("value"), athena_row("first")],
            ),
            result_page(columns, [athena_row("second")]),
            result_page(columns, [athena_row("third")]),
        ],
    )

    actual = execute(client)

    assert actual.to_dict(orient="records") == [
        {"value": "first"},
        {"value": "second"},
        {"value": "third"},
    ]
    assert client.paginator.calls == [{"QueryExecutionId": QUERY_ID}]
