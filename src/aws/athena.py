"""Reusable AWS Athena query execution and result parsing."""

import time
from dataclasses import dataclass

import boto3
import pandas as pd


class AthenaQueryError(RuntimeError):
    """Raised when Athena fails or cancels a query execution."""


def create_athena_client(region_name: str):
    """Create an Athena client in the configured AWS region."""

    return boto3.client(
        "athena",
        region_name=region_name,
    )


@dataclass(frozen=True)
class QueryStats:
    """Cost and timing of one Athena query execution."""

    query_execution_id: str
    bytes_scanned: int
    engine_ms: int


def execute_query(
    client,
    *,
    query: str,
    database: str,
    output_location: str,
    poll_interval_seconds: float = 0.5,
) -> pd.DataFrame:
    """Execute an Athena query and return its string-valued result frame."""

    frame, _ = execute_query_with_stats(
        client,
        query=query,
        database=database,
        output_location=output_location,
        poll_interval_seconds=poll_interval_seconds,
    )
    return frame


def execute_query_with_stats(
    client,
    *,
    query: str,
    database: str,
    output_location: str,
    poll_interval_seconds: float = 0.5,
) -> tuple[pd.DataFrame, QueryStats]:
    """Execute an Athena query and return its result frame and scan statistics."""

    response = client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={
            "Database": database,
        },
        ResultConfiguration={
            "OutputLocation": output_location,
        },
    )
    query_execution_id = response["QueryExecutionId"]

    while True:
        response = client.get_query_execution(
            QueryExecutionId=query_execution_id,
        )
        status = response["QueryExecution"]["Status"]
        state = status["State"]

        if state == "SUCCEEDED":
            statistics = response["QueryExecution"].get("Statistics", {})
            stats = QueryStats(
                query_execution_id=query_execution_id,
                bytes_scanned=int(statistics.get("DataScannedInBytes", 0)),
                engine_ms=int(statistics.get("EngineExecutionTimeInMillis", 0)),
            )
            break

        if state in {"FAILED", "CANCELLED"}:
            reason = status.get(
                "StateChangeReason",
                "Unknown Athena error",
            )
            raise AthenaQueryError(
                f"Athena query {query_execution_id} {state}: {reason}"
            )

        time.sleep(poll_interval_seconds)

    paginator = client.get_paginator("get_query_results")
    rows = []
    columns = []
    first_page = True

    for page in paginator.paginate(
        QueryExecutionId=query_execution_id,
    ):
        result_set = page["ResultSet"]

        if first_page:
            columns = [
                column["Name"]
                for column in result_set["ResultSetMetadata"]["ColumnInfo"]
            ]

        page_rows = result_set["Rows"]

        # Athena returns the header as the first row of the first page.
        if first_page:
            page_rows = page_rows[1:]
            first_page = False

        for row in page_rows:
            values = [
                item.get("VarCharValue", None)
                for item in row["Data"]
            ]

            # Athena can omit trailing null columns.
            while len(values) < len(columns):
                values.append(None)

            rows.append(values)

    return (
        pd.DataFrame(
            rows,
            columns=columns,
        ),
        stats,
    )
