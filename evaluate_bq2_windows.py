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

INPUT_PATH = Path("bq2_random_evaluation_cases.csv")
SUMMARY_OUTPUT_PATH = Path("bq2_window_tradeoff_evaluation.csv")
DETAIL_OUTPUT_PATH = Path("bq2_window_case_results.csv")

WINDOWS_HOURS = (6, 12, 24, 30, 48, 72)
MAX_WINDOW_MINUTES = max(WINDOWS_HOURS) * 60
POLL_INTERVAL_SECONDS = 0.5

REQUIRED_COLUMNS = {
    "pattern_id",
    "alert_position",
    "alert_timestamp",
    "alert_sender",
    "alert_receiver",
    "expected_upstream_account",
    "expected_downstream_account",
    "required_window_minutes",
}


class EvaluationError(RuntimeError):
    """Raised when the window evaluation cannot be completed safely."""


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
    cases["alert_position"] = pd.to_numeric(
        cases["alert_position"],
        errors="raise",
    ).astype(int)
    cases["required_window_minutes"] = pd.to_numeric(
        cases["required_window_minutes"],
        errors="raise",
    ).astype(int)

    identifier_columns = (
        "pattern_id",
        "alert_sender",
        "alert_receiver",
        "expected_upstream_account",
        "expected_downstream_account",
    )
    if cases[list(identifier_columns)].isna().any().any():
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
                    f"TIMESTAMP {sql_string(timestamp)}",
                    sql_string(case.alert_sender),
                    sql_string(case.alert_receiver),
                )
            ) + ")"
        )
    return ",\n        ".join(rows)


def build_query(cases: pd.DataFrame) -> str:
    values_clause = build_values_clause(cases)
    earliest_date = (
        cases["alert_timestamp"].min()
        - timedelta(minutes=MAX_WINDOW_MINUTES)
    ).date()
    latest_date = (
        cases["alert_timestamp"].max()
        + timedelta(minutes=MAX_WINDOW_MINUTES)
    ).date()

    return f"""
WITH evaluation_cases (
    case_id,
    alert_timestamp,
    alert_sender,
    alert_receiver
) AS (
    VALUES
        {values_clause}
),
upstream_edges AS (
    SELECT
        e.case_id,
        'UPSTREAM' AS direction,
        g.source_account_key AS connected_account,
        g.transaction_id,
        DATE_DIFF(
            'minute',
            g.transaction_timestamp,
            e.alert_timestamp
        ) AS gap_minutes
    FROM {ATHENA_TABLE} g
    INNER JOIN evaluation_cases e
        ON g.target_account_key = e.alert_sender
        AND g.txn_date BETWEEN
            CAST(
                DATE_ADD('minute', -{MAX_WINDOW_MINUTES}, e.alert_timestamp)
                AS DATE
            )
            AND CAST(e.alert_timestamp AS DATE)
        AND g.transaction_timestamp <= e.alert_timestamp
        AND DATE_DIFF(
            'minute',
            g.transaction_timestamp,
            e.alert_timestamp
        ) <= {MAX_WINDOW_MINUTES}
        AND NOT (
            g.source_account_key = e.alert_sender
            AND g.target_account_key = e.alert_receiver
            AND g.transaction_timestamp = e.alert_timestamp
        )
    WHERE g.txn_date BETWEEN DATE '{earliest_date}' AND DATE '{latest_date}'
),
downstream_edges AS (
    SELECT
        e.case_id,
        'DOWNSTREAM' AS direction,
        g.target_account_key AS connected_account,
        g.transaction_id,
        DATE_DIFF(
            'minute',
            e.alert_timestamp,
            g.transaction_timestamp
        ) AS gap_minutes
    FROM {ATHENA_TABLE} g
    INNER JOIN evaluation_cases e
        ON g.source_account_key = e.alert_receiver
        AND g.txn_date BETWEEN
            CAST(e.alert_timestamp AS DATE)
            AND CAST(
                DATE_ADD('minute', {MAX_WINDOW_MINUTES}, e.alert_timestamp)
                AS DATE
            )
        AND g.transaction_timestamp >= e.alert_timestamp
        AND DATE_DIFF(
            'minute',
            e.alert_timestamp,
            g.transaction_timestamp
        ) <= {MAX_WINDOW_MINUTES}
        AND NOT (
            g.source_account_key = e.alert_sender
            AND g.target_account_key = e.alert_receiver
            AND g.transaction_timestamp = e.alert_timestamp
        )
    WHERE g.txn_date BETWEEN DATE '{earliest_date}' AND DATE '{latest_date}'
)
SELECT
    case_id,
    direction,
    connected_account,
    transaction_id,
    gap_minutes
FROM upstream_edges

UNION ALL

SELECT
    case_id,
    direction,
    connected_account,
    transaction_id,
    gap_minutes
FROM downstream_edges
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


def build_case_window_results(
    cases: pd.DataFrame,
    retrieved: pd.DataFrame,
) -> pd.DataFrame:
    if retrieved.empty:
        retrieved = pd.DataFrame(
            columns=(
                "case_id",
                "direction",
                "connected_account",
                "transaction_id",
                "gap_minutes",
            )
        )
    else:
        retrieved = retrieved.copy()
        retrieved["gap_minutes"] = pd.to_numeric(
            retrieved["gap_minutes"],
            errors="raise",
        ).astype(int)

    retrieved_by_case = {
        case_id: group
        for case_id, group in retrieved.groupby("case_id", sort=False)
    }
    empty_edges = retrieved.iloc[0:0]
    rows = []

    for window_hours in WINDOWS_HOURS:
        window_minutes = window_hours * 60

        for case in cases.itertuples(index=False):
            case_edges = retrieved_by_case.get(case.case_id, empty_edges)
            window_edges = case_edges[
                case_edges["gap_minutes"] <= window_minutes
            ]
            upstream_edges = window_edges[
                window_edges["direction"] == "UPSTREAM"
            ]
            downstream_edges = window_edges[
                window_edges["direction"] == "DOWNSTREAM"
            ]

            upstream_accounts = set(
                upstream_edges["connected_account"].dropna()
            )
            downstream_accounts = set(
                downstream_edges["connected_account"].dropna()
            )
            total_accounts = upstream_accounts | downstream_accounts

            upstream_found = int(
                case.expected_upstream_account in upstream_accounts
            )
            downstream_found = int(
                case.expected_downstream_account in downstream_accounts
            )
            recovered = upstream_found + downstream_found
            both_found = int(recovered == 2)
            timing_ceiling_eligible = int(
                case.required_window_minutes <= window_minutes
            )

            rows.append(
                {
                    "case_id": case.case_id,
                    "pattern_id": case.pattern_id,
                    "alert_position": case.alert_position,
                    "window_hours": window_hours,
                    "upstream_found": upstream_found,
                    "downstream_found": downstream_found,
                    "both_found": both_found,
                    "at_least_one_found": int(recovered >= 1),
                    "expected_accounts_recovered": recovered,
                    "upstream_candidate_accounts": len(upstream_accounts),
                    "downstream_candidate_accounts": len(downstream_accounts),
                    "total_candidate_accounts": len(total_accounts),
                    "upstream_transaction_edges": len(upstream_edges),
                    "downstream_transaction_edges": len(downstream_edges),
                    "total_connected_transaction_edges": len(window_edges),
                    "ground_truth_neighbour_concentration": (
                        recovered / len(total_accounts)
                        if total_accounts
                        else float("nan")
                    ),
                    "timing_ceiling_eligible": timing_ceiling_eligible,
                    "unexpected_timing_eligible_miss": int(
                        timing_ceiling_eligible == 1 and both_found == 0
                    ),
                }
            )

    return pd.DataFrame(rows)


def build_window_summary(case_windows: pd.DataFrame) -> pd.DataFrame:
    rows = []

    for window_hours in WINDOWS_HOURS:
        window_cases = case_windows[
            case_windows["window_hours"] == window_hours
        ]
        total = len(window_cases)
        candidates = window_cases["total_candidate_accounts"]
        transaction_edges = window_cases["total_connected_transaction_edges"]
        concentration = window_cases[
            "ground_truth_neighbour_concentration"
        ].dropna()

        both_found_count = int(window_cases["both_found"].sum())
        both_found_pct = both_found_count / total * 100
        timing_ceiling_count = int(
            window_cases["timing_ceiling_eligible"].sum()
        )
        timing_ceiling_pct = timing_ceiling_count / total * 100

        rows.append(
            {
                "window_hours": window_hours,
                "both_found_count": both_found_count,
                "both_found_pct": both_found_pct,
                "at_least_one_found_pct": (
                    window_cases["at_least_one_found"].mean() * 100
                ),
                "upstream_recovery_pct": (
                    window_cases["upstream_found"].mean() * 100
                ),
                "downstream_recovery_pct": (
                    window_cases["downstream_found"].mean() * 100
                ),
                "candidate_accounts_median": candidates.median(),
                "candidate_accounts_mean": candidates.mean(),
                "candidate_accounts_p75": candidates.quantile(0.75),
                "candidate_accounts_p90": candidates.quantile(0.90),
                "candidate_accounts_p95": candidates.quantile(0.95),
                "candidate_accounts_max": candidates.max(),
                "transaction_edges_median": transaction_edges.median(),
                "transaction_edges_mean": transaction_edges.mean(),
                "transaction_edges_p90": transaction_edges.quantile(0.90),
                "transaction_edges_p95": transaction_edges.quantile(0.95),
                "transaction_edges_max": transaction_edges.max(),
                "ground_truth_neighbour_concentration_median": (
                    concentration.median()
                ),
                "ground_truth_neighbour_concentration_mean": concentration.mean(),
                "theoretical_timing_ceiling_count": timing_ceiling_count,
                "theoretical_timing_ceiling_pct": timing_ceiling_pct,
                "timing_ceiling_gap_pct": timing_ceiling_pct - both_found_pct,
                "unexpected_timing_eligible_misses": int(
                    window_cases["unexpected_timing_eligible_miss"].sum()
                ),
            }
        )

    summary = pd.DataFrame(rows)
    summary["additional_both_found_vs_previous_window"] = (
        summary["both_found_count"].diff()
    )
    summary["additional_median_candidates_vs_previous_window"] = (
        summary["candidate_accounts_median"].diff()
    )
    summary["cases_with_both_found_per_median_candidate"] = (
        summary["both_found_pct"]
        / summary["candidate_accounts_median"].replace(0, float("nan"))
    )
    return summary


def print_athena_metadata(execution: AthenaExecution):
    print("Athena execution metadata")
    print(f"  QueryExecutionId: {execution.query_execution_id}")
    print(f"  Status: {execution.status}")
    if execution.execution_time_ms is not None:
        print(f"  Execution time: {execution.execution_time_ms} ms")
    print(f"  Bytes scanned: {execution.data_scanned_bytes}")
    print(
        "  GB scanned: "
        f"{execution.data_scanned_bytes / 1_073_741_824:.6f}"
    )
    print("  Total Athena queries: 1")


def print_summary(summary: pd.DataFrame, case_windows: pd.DataFrame):
    compact = summary[
        [
            "window_hours",
            "both_found_count",
            "both_found_pct",
            "candidate_accounts_median",
            "candidate_accounts_p90",
            "ground_truth_neighbour_concentration_median",
        ]
    ].copy()
    compact.columns = (
        "Window",
        "Both count",
        "Both %",
        "Median candidates",
        "P90 candidates",
        "Median concentration",
    )

    print("Recovery-versus-noise summary")
    print(compact.to_string(index=False, float_format=lambda value: f"{value:.2f}"))

    print("Incremental trade-off statistics")
    for index, row in summary.iterrows():
        efficiency = row["cases_with_both_found_per_median_candidate"]
        if index == 0:
            print(
                f"  {int(row['window_hours'])}h: baseline; "
                "project-specific both-found efficiency="
                f"{efficiency:.4f}"
            )
            continue

        previous_hours = int(summary.iloc[index - 1]["window_hours"])
        print(
            f"  {previous_hours}h -> {int(row['window_hours'])}h: "
            "+"
            f"{int(row['additional_both_found_vs_previous_window'])} "
            "both-found cases; median candidates change="
            f"{row['additional_median_candidates_vs_previous_window']:+.2f}; "
            "project-specific both-found efficiency="
            f"{efficiency:.4f}"
        )

    print("Timing ceiling comparison")
    for row in summary.itertuples(index=False):
        print(
            f"  {row.window_hours}h: theoretical="
            f"{row.theoretical_timing_ceiling_pct:.2f}%; actual="
            f"{row.both_found_pct:.2f}%; gap={row.timing_ceiling_gap_pct:.2f} "
            "percentage points"
        )

    unexpected = case_windows[
        case_windows["unexpected_timing_eligible_miss"] == 1
    ]
    if unexpected.empty:
        print("Unexpected inside-window misses: none")
    else:
        print("Unexpected inside-window misses:")
        for case in unexpected.itertuples(index=False):
            print(f"  {case.case_id} at {case.window_hours}h")


def main() -> int:
    try:
        cases = load_cases()
        session = boto3.Session(region_name=AWS_REGION)
        if session.get_credentials() is None:
            raise NoCredentialsError()
        athena = session.client("athena")

        query_execution_id = run_athena_query(
            athena,
            build_query(cases),
        )
        query_execution = wait_for_query(athena, query_execution_id)
        retrieved = fetch_results(athena, query_execution_id)
        execution = execution_metadata(query_execution_id, query_execution)

        case_windows = build_case_window_results(cases, retrieved)
        summary = build_window_summary(case_windows)
        summary.to_csv(SUMMARY_OUTPUT_PATH, index=False)
        case_windows.to_csv(DETAIL_OUTPUT_PATH, index=False)

        print_athena_metadata(execution)
        print_summary(summary, case_windows)
        print(f"Saved window summary to: {SUMMARY_OUTPUT_PATH}")
        print(f"Saved detailed case-window results to: {DETAIL_OUTPUT_PATH}")
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
