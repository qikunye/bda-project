import argparse
import re
import sys
import time

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
)

REGION = "us-east-1"
DATABASE = "is459_aml_gold"
TABLE = "is459_aml_gold.bq2_graph_edges_medium_v1"
OUTPUT_LOCATION = "s3://is459-aml-g1t5/athena-results/bq2/"
DEFAULT_TRANSACTION_ID = "txn_630dea1dda9482dbd765a966355d6708"
POLL_INTERVAL_SECONDS = 1

CREDENTIAL_ERROR_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "InvalidSignatureException",
    "RequestExpired",
    "SignatureDoesNotMatch",
    "UnrecognizedClientException",
}


class SmokeTestError(RuntimeError):
    """Raised when an expected smoke-test assertion fails."""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a lightweight Athena lookup against the AML Gold table."
    )
    parser.add_argument(
        "transaction_id",
        nargs="?",
        default=DEFAULT_TRANSACTION_ID,
        help=f"Transaction ID to look up (default: {DEFAULT_TRANSACTION_ID})",
    )
    return parser.parse_args()


def build_query(transaction_id: str) -> str:
    if not re.fullmatch(r"txn_[A-Za-z0-9]+", transaction_id):
        raise SmokeTestError(
            "Invalid transaction ID. Expected 'txn_' followed by letters or digits."
        )

    return f"""
SELECT
    transaction_id,
    source_account_key,
    target_account_key,
    transaction_timestamp,
    amount_paid,
    payment_currency,
    payment_format
FROM {TABLE}
WHERE transaction_id = '{transaction_id}'
LIMIT 2
"""


def run_query(athena, transaction_id: str) -> str:
    response = athena.start_query_execution(
        QueryString=build_query(transaction_id),
        QueryExecutionContext={"Database": DATABASE},
        ResultConfiguration={"OutputLocation": OUTPUT_LOCATION},
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
            raise SmokeTestError(f"Athena query {state}: {reason}")

        time.sleep(POLL_INTERVAL_SECONDS)


def fetch_results(athena, query_execution_id: str) -> list[dict]:
    response = athena.get_query_results(
        QueryExecutionId=query_execution_id,
        MaxResults=3,
    )
    result_set = response["ResultSet"]
    columns = [
        column["Name"]
        for column in result_set["ResultSetMetadata"]["ColumnInfo"]
    ]

    rows = []
    for result_row in result_set.get("Rows", [])[1:]:
        values = [
            item.get("VarCharValue")
            for item in result_row.get("Data", [])
        ]
        values.extend([None] * (len(columns) - len(values)))
        rows.append(dict(zip(columns, values, strict=True)))

    return rows


def print_execution_metadata(query_execution: dict):
    status = query_execution["Status"]["State"]
    statistics = query_execution.get("Statistics", {})
    scanned_bytes = statistics.get("DataScannedInBytes", 0)

    print(f"Athena engine status: {status}")

    execution_ms = statistics.get("EngineExecutionTimeInMillis")
    if execution_ms is not None:
        print(f"Execution time: {execution_ms} ms")

    print(f"Data scanned: {scanned_bytes} bytes ({scanned_bytes / 1_048_576:.3f} MB)")


def print_transaction(row: dict):
    print("Matched transaction:")
    for field in (
        "transaction_id",
        "source_account_key",
        "target_account_key",
        "transaction_timestamp",
        "amount_paid",
        "payment_currency",
        "payment_format",
    ):
        print(f"  {field}: {row[field]}")


def is_credential_client_error(error: ClientError) -> bool:
    code = error.response.get("Error", {}).get("Code", "")
    return code in CREDENTIAL_ERROR_CODES


def main() -> int:
    args = parse_args()

    try:
        session = boto3.Session(region_name=REGION)
        if session.get_credentials() is None:
            raise NoCredentialsError()

        athena = session.client("athena")
        query_execution_id = run_query(athena, args.transaction_id)
        print(f"QueryExecutionId: {query_execution_id}")

        query_execution = wait_for_query(athena, query_execution_id)
        rows = fetch_results(athena, query_execution_id)

        print_execution_metadata(query_execution)

        if not rows:
            raise SmokeTestError(
                f"Transaction ID does not exist: {args.transaction_id}"
            )

        if len(rows) > 1:
            raise SmokeTestError(
                "Expected one row for unique transaction ID "
                f"{args.transaction_id}, but found at least {len(rows)}."
            )

        print_transaction(rows[0])
        print("PASS: Athena connection and Gold-table lookup succeeded")
        return 0

    except (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError) as error:
        print(f"FAIL: AWS credentials are unavailable: {error}", file=sys.stderr)
    except ClientError as error:
        if is_credential_client_error(error):
            print(f"FAIL: AWS credentials are unavailable: {error}", file=sys.stderr)
        else:
            print(f"FAIL: AWS API error: {error}", file=sys.stderr)
    except SmokeTestError as error:
        print(f"FAIL: {error}", file=sys.stderr)
    except BotoCoreError as error:
        print(f"FAIL: AWS SDK error: {error}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())
