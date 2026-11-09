import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import boto3
import pandas as pd
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
)

AWS_REGION = "us-east-1"
ATHENA_DATABASE = "is459_aml_gold"
ATHENA_TABLE = "is459_aml_gold.bq2_graph_edges_medium_v1"
ATHENA_OUTPUT = "s3://is459-aml-g1t5/athena-results/bq2/"

EVAL_DIR = Path(__file__).resolve().parent
INPUT_PATH = EVAL_DIR / "bq2_random_evaluation_cases.csv"
OUTPUT_PATH = EVAL_DIR / "bq2_30h_athena_evaluation.csv"
WINDOW_MINUTES = 1800
BATCH_SIZE = 200
POLL_INTERVAL_SECONDS = 0.5

FAILURE_OUTSIDE_WINDOW = (
    "expected account lies outside the 30-hour ground-truth timing window"
)
FAILURE_INSIDE_NOT_RETRIEVED = (
    "expected account should be inside 30 hours but was not retrieved"
)
FAILURE_OTHER = "another identifiable data/query issue"

REQUIRED_COLUMNS = {
    "pattern_id",
    "alert_position",
    "alert_timestamp",
    "alert_sender",
    "alert_receiver",
    "expected_upstream_account",
    "expected_downstream_account",
    "upstream_gap_minutes",
    "downstream_gap_minutes",
    "required_window_minutes",
}


class EvaluationError(RuntimeError):
    """Raised when the BQ2 evaluation cannot be completed safely."""


@dataclass(frozen=True)
class AthenaExecution:
    query_execution_id: str
    status: str
    execution_time_ms: int | None
    data_scanned_bytes: int


def load_cases(path: Path = INPUT_PATH) -> pd.DataFrame:
    cases = pd.read_csv(path)
    missing_columns = REQUIRED_COLUMNS - set(cases.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise EvaluationError(f"Input CSV is missing required columns: {missing}")
    if cases.empty:
        raise EvaluationError("Input CSV contains no evaluation cases")

    cases = cases.copy()
    cases["alert_timestamp"] = pd.to_datetime(
        cases["alert_timestamp"],
        format="%Y-%m-%d %H:%M:%S",
        errors="raise",
    )

    numeric_columns = (
        "alert_position",
        "upstream_gap_minutes",
        "downstream_gap_minutes",
        "required_window_minutes",
    )
    for column in numeric_columns:
        cases[column] = pd.to_numeric(cases[column], errors="raise").astype(int)

    string_columns = (
        "pattern_id",
        "alert_sender",
        "alert_receiver",
        "expected_upstream_account",
        "expected_downstream_account",
    )
    if cases[list(string_columns)].isna().any().any():
        raise EvaluationError("Input CSV contains missing case identifiers or accounts")

    cases.insert(
        0,
        "case_id",
        cases["pattern_id"] + "-" + cases["alert_position"].astype(str),
    )
    if cases["case_id"].duplicated().any():
        duplicates = cases.loc[cases["case_id"].duplicated(), "case_id"].tolist()
        raise EvaluationError(f"Duplicate case IDs found: {duplicates}")

    return cases


def sql_string(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def build_values_clause(cases: pd.DataFrame) -> str:
    rows = []
    for case in cases.itertuples(index=False):
        timestamp = case.alert_timestamp.strftime("%Y-%m-%d %H:%M:%S")
        rows.append(
            "(" + ", ".join(
                (
                    sql_string(case.case_id),
                    sql_string(case.pattern_id),
                    str(int(case.alert_position)),
                    f"TIMESTAMP {sql_string(timestamp)}",
                    sql_string(case.alert_sender),
                    sql_string(case.alert_receiver),
                    sql_string(case.expected_upstream_account),
                    sql_string(case.expected_downstream_account),
                )
            ) + ")"
        )
    return ",\n        ".join(rows)


def build_batch_query(cases: pd.DataFrame) -> str:
    values_clause = build_values_clause(cases)
    earliest_date = (
        cases["alert_timestamp"].min() - timedelta(minutes=WINDOW_MINUTES)
    ).date()
    latest_date = (
        cases["alert_timestamp"].max() + timedelta(minutes=WINDOW_MINUTES)
    ).date()

    return f"""
WITH evaluation_cases (
    case_id,
    pattern_id,
    alert_position,
    alert_timestamp,
    alert_sender,
    alert_receiver,
    expected_upstream_account,
    expected_downstream_account
) AS (
    VALUES
        {values_clause}
),
upstream_candidates AS (
    SELECT
        e.case_id,
        'UPSTREAM' AS edge_role,
        g.source_account_key AS connected_account_key,
        COUNT(*) AS transaction_edges
    FROM {ATHENA_TABLE} g
    INNER JOIN evaluation_cases e
        ON g.target_account_key = e.alert_sender
        AND g.txn_date BETWEEN
            CAST(DATE_ADD('minute', -{WINDOW_MINUTES}, e.alert_timestamp) AS DATE)
            AND CAST(e.alert_timestamp AS DATE)
        AND g.transaction_timestamp <= e.alert_timestamp
        AND DATE_DIFF('minute', g.transaction_timestamp, e.alert_timestamp)
            <= {WINDOW_MINUTES}
    WHERE g.txn_date BETWEEN DATE '{earliest_date}' AND DATE '{latest_date}'
    GROUP BY e.case_id, g.source_account_key
),
downstream_candidates AS (
    SELECT
        e.case_id,
        'DOWNSTREAM' AS edge_role,
        g.target_account_key AS connected_account_key,
        COUNT(*) AS transaction_edges
    FROM {ATHENA_TABLE} g
    INNER JOIN evaluation_cases e
        ON g.source_account_key = e.alert_receiver
        AND g.txn_date BETWEEN
            CAST(e.alert_timestamp AS DATE)
            AND CAST(DATE_ADD('minute', {WINDOW_MINUTES}, e.alert_timestamp) AS DATE)
        AND g.transaction_timestamp >= e.alert_timestamp
        AND DATE_DIFF('minute', e.alert_timestamp, g.transaction_timestamp)
            <= {WINDOW_MINUTES}
    WHERE g.txn_date BETWEEN DATE '{earliest_date}' AND DATE '{latest_date}'
    GROUP BY e.case_id, g.target_account_key
)
SELECT
    case_id,
    edge_role,
    connected_account_key,
    transaction_edges
FROM upstream_candidates

UNION ALL

SELECT
    case_id,
    edge_role,
    connected_account_key,
    transaction_edges
FROM downstream_candidates
"""


def run_athena_query(athena, query: str) -> str:
    response = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": ATHENA_DATABASE},
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
    )
    return response["QueryExecutionId"]


def wait_for_query(athena, query_execution_id: str) -> dict:
    while True:
        response = athena.get_query_execution(
            QueryExecutionId=query_execution_id
        )
        query_execution = response["QueryExecution"]
        status = query_execution["Status"]
        state = status["State"]

        if state == "SUCCEEDED":
            return query_execution
        if state in {"FAILED", "CANCELLED"}:
            reason = status.get("StateChangeReason", "No reason supplied")
            raise EvaluationError(
                f"Athena query {query_execution_id} {state}: {reason}"
            )

        time.sleep(POLL_INTERVAL_SECONDS)


def fetch_results(athena, query_execution_id: str) -> pd.DataFrame:
    paginator = athena.get_paginator("get_query_results")
    columns = []
    rows = []
    first_page = True

    for page in paginator.paginate(QueryExecutionId=query_execution_id):
        result_set = page["ResultSet"]
        if first_page:
            columns = [
                column["Name"]
                for column in result_set["ResultSetMetadata"]["ColumnInfo"]
            ]

        page_rows = result_set.get("Rows", [])
        if first_page:
            page_rows = page_rows[1:]
            first_page = False

        for result_row in page_rows:
            values = [
                item.get("VarCharValue")
                for item in result_row.get("Data", [])
            ]
            values.extend([None] * (len(columns) - len(values)))
            rows.append(values)

    return pd.DataFrame(rows, columns=columns)


def execution_metadata(query_execution_id: str, query_execution: dict) -> AthenaExecution:
    statistics = query_execution.get("Statistics", {})
    return AthenaExecution(
        query_execution_id=query_execution_id,
        status=query_execution["Status"]["State"],
        execution_time_ms=statistics.get("EngineExecutionTimeInMillis"),
        data_scanned_bytes=statistics.get("DataScannedInBytes", 0),
    )


def print_execution(execution: AthenaExecution, query_number: int):
    scanned_mb = execution.data_scanned_bytes / 1_048_576
    scanned_gb = execution.data_scanned_bytes / 1_073_741_824
    print(f"Athena query {query_number}")
    print(f"  QueryExecutionId: {execution.query_execution_id}")
    print(f"  Status: {execution.status}")
    if execution.execution_time_ms is not None:
        print(f"  Execution time: {execution.execution_time_ms} ms")
    print(
        f"  Data scanned: {execution.data_scanned_bytes} bytes "
        f"({scanned_mb:.3f} MB, {scanned_gb:.6f} GB)"
    )


def evaluate_batch(
    athena,
    cases: pd.DataFrame,
    query_number: int,
) -> tuple[pd.DataFrame, AthenaExecution]:
    query = build_batch_query(cases)
    query_execution_id = run_athena_query(athena, query)
    query_execution = wait_for_query(athena, query_execution_id)
    results = fetch_results(athena, query_execution_id)
    execution = execution_metadata(query_execution_id, query_execution)
    print_execution(execution, query_number)
    return results, execution


def classify_failure(case) -> str | None:
    if case.both_found:
        return None

    missing_gaps = []
    if not case.upstream_found:
        missing_gaps.append(case.upstream_gap_minutes)
    if not case.downstream_found:
        missing_gaps.append(case.downstream_gap_minutes)

    if not missing_gaps or any(pd.isna(gap) or gap < 0 for gap in missing_gaps):
        return FAILURE_OTHER
    if any(gap <= WINDOW_MINUTES for gap in missing_gaps):
        return FAILURE_INSIDE_NOT_RETRIEVED
    return FAILURE_OUTSIDE_WINDOW


def build_case_metrics(cases: pd.DataFrame, retrieved: pd.DataFrame) -> pd.DataFrame:
    if retrieved.empty:
        retrieved = pd.DataFrame(
            columns=(
                "case_id",
                "edge_role",
                "connected_account_key",
                "transaction_edges",
            )
        )
    else:
        retrieved = retrieved.copy()
        retrieved["transaction_edges"] = pd.to_numeric(
            retrieved["transaction_edges"],
            errors="raise",
        ).astype(int)

    retrieved_by_case = {
        case_id: group
        for case_id, group in retrieved.groupby("case_id", sort=False)
    }
    metric_rows = []

    for case in cases.itertuples(index=False):
        case_edges = retrieved_by_case.get(
            case.case_id,
            retrieved.iloc[0:0],
        )
        upstream_edges = case_edges[case_edges["edge_role"] == "UPSTREAM"]
        downstream_edges = case_edges[case_edges["edge_role"] == "DOWNSTREAM"]

        upstream_accounts = set(
            upstream_edges["connected_account_key"].dropna()
        )
        downstream_accounts = set(
            downstream_edges["connected_account_key"].dropna()
        )
        total_accounts = upstream_accounts | downstream_accounts

        upstream_found = int(case.expected_upstream_account in upstream_accounts)
        downstream_found = int(
            case.expected_downstream_account in downstream_accounts
        )
        expected_accounts_recovered = upstream_found + downstream_found

        metric_rows.append(
            {
                "case_id": case.case_id,
                "upstream_found": upstream_found,
                "downstream_found": downstream_found,
                "both_found": int(upstream_found == 1 and downstream_found == 1),
                "upstream_candidate_accounts": len(upstream_accounts),
                "downstream_candidate_accounts": len(downstream_accounts),
                "total_candidate_accounts": len(total_accounts),
                "upstream_transaction_edges": int(
                    upstream_edges["transaction_edges"].sum()
                ),
                "downstream_transaction_edges": int(
                    downstream_edges["transaction_edges"].sum()
                ),
                "total_transaction_edges": int(
                    case_edges["transaction_edges"].sum()
                ),
                "expected_accounts_recovered": expected_accounts_recovered,
                "ground_truth_neighbour_concentration": (
                    expected_accounts_recovered / len(total_accounts)
                    if total_accounts
                    else float("nan")
                ),
            }
        )

    metrics = pd.DataFrame(metric_rows)
    detailed = cases.merge(metrics, on="case_id", how="left", validate="one_to_one")
    detailed["failure_reason"] = [
        classify_failure(case)
        for case in detailed.itertuples(index=False)
    ]
    return detailed


def print_distribution(label: str, values: pd.Series):
    print(label)
    print(f"  Minimum: {values.min():.2f}")
    print(f"  Median: {values.median():.2f}")
    print(f"  Mean: {values.mean():.2f}")
    print(f"  75th percentile: {values.quantile(0.75):.2f}")
    print(f"  90th percentile: {values.quantile(0.90):.2f}")
    print(f"  95th percentile: {values.quantile(0.95):.2f}")
    print(f"  Maximum: {values.max():.2f}")


def print_count_and_percentage(label: str, count: int, total: int):
    percentage = count / total * 100
    print(f"{label}: {count}/{total} ({percentage:.2f}%)")


def print_summary(
    detailed: pd.DataFrame,
    executions: list[AthenaExecution],
):
    total = len(detailed)
    upstream_count = int(detailed["upstream_found"].sum())
    downstream_count = int(detailed["downstream_found"].sum())
    both_count = int(detailed["both_found"].sum())
    at_least_one_count = int(
        (detailed["expected_accounts_recovered"] >= 1).sum()
    )
    timing_ceiling = int(
        (detailed["required_window_minutes"] <= WINDOW_MINUTES).sum()
    )

    print("BQ2 30-hour Athena evaluation summary")
    print("Recovery")
    print(f"Total evaluation cases: {total}")
    print_count_and_percentage("Upstream account recovery", upstream_count, total)
    print_count_and_percentage("Downstream account recovery", downstream_count, total)
    print_count_and_percentage("Both found", both_count, total)
    print_count_and_percentage("At least one found", at_least_one_count, total)
    print(
        "Average expected accounts recovered out of 2: "
        f"{detailed['expected_accounts_recovered'].mean():.4f}"
    )
    print_count_and_percentage(
        "Timing-only 30-hour both-found ceiling",
        timing_ceiling,
        total,
    )
    print(f"Actual both-found gap to timing ceiling: {timing_ceiling - both_count}")

    print_distribution(
        "Candidate-set size: total_candidate_accounts",
        detailed["total_candidate_accounts"],
    )
    print_distribution(
        "Transaction edges: total_transaction_edges",
        detailed["total_transaction_edges"],
    )

    concentration = detailed["ground_truth_neighbour_concentration"].dropna()
    print("Ground-truth neighbour concentration")
    print(f"  Median: {concentration.median():.4f}")
    print(f"  Mean: {concentration.mean():.4f}")
    print(f"  25th percentile: {concentration.quantile(0.25):.4f}")
    print(f"  75th percentile: {concentration.quantile(0.75):.4f}")

    failures = detailed[detailed["both_found"] == 0]
    failure_counts = failures["failure_reason"].value_counts()
    print("Failure analysis")
    for reason in (
        FAILURE_OUTSIDE_WINDOW,
        FAILURE_INSIDE_NOT_RETRIEVED,
        FAILURE_OTHER,
    ):
        print(f"  {reason}: {int(failure_counts.get(reason, 0))}")

    total_scanned_bytes = sum(
        execution.data_scanned_bytes
        for execution in executions
    )
    print("Athena execution totals")
    print(f"  Athena queries executed: {len(executions)}")
    print(f"  Total bytes scanned: {total_scanned_bytes}")
    print(f"  Total GB scanned: {total_scanned_bytes / 1_073_741_824:.6f}")


def main() -> int:
    try:
        cases = load_cases()
        session = boto3.Session(region_name=AWS_REGION)
        if session.get_credentials() is None:
            raise NoCredentialsError()
        athena = session.client("athena")

        result_frames = []
        executions = []
        for query_number, start in enumerate(
            range(0, len(cases), BATCH_SIZE),
            start=1,
        ):
            batch = cases.iloc[start:start + BATCH_SIZE]
            results, execution = evaluate_batch(
                athena,
                batch,
                query_number,
            )
            result_frames.append(results)
            executions.append(execution)

        retrieved = pd.concat(result_frames, ignore_index=True)
        detailed = build_case_metrics(cases, retrieved)
        detailed.to_csv(OUTPUT_PATH, index=False)

        print_summary(detailed, executions)
        print(f"Saved detailed evaluation to: {OUTPUT_PATH}")
        return 0

    except (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError) as error:
        print(f"ERROR: AWS credentials are unavailable: {error}", file=sys.stderr)
    except ClientError as error:
        print(f"ERROR: Athena API request failed: {error}", file=sys.stderr)
    except (BotoCoreError, EvaluationError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())
