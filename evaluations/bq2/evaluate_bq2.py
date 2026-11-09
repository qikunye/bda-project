import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
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
S3_BUCKET = "is459-aml-g1t5"
S3_KEY = "bronze/ibm-aml/li-medium/patterns/LI-Medium_Patterns.txt"
EVAL_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = EVAL_DIR / "bq2_random_evaluation_cases.csv"

RANDOM_HEADER_PREFIX = "BEGIN LAUNDERING ATTEMPT - RANDOM"
RANDOM_END_MARKER = "END LAUNDERING ATTEMPT - RANDOM"
ANY_BLOCK_HEADER_PREFIX = "BEGIN LAUNDERING ATTEMPT - "
ANY_BLOCK_END_PREFIX = "END LAUNDERING ATTEMPT - "
FIXED_WINDOWS_HOURS = (6, 12, 24, 30, 48, 72)

OUTPUT_COLUMNS = [
    "pattern_id",
    "pattern_type",
    "chain_length_transactions",
    "alert_position",
    "alert_timestamp",
    "alert_sender",
    "alert_receiver",
    "amount_paid",
    "payment_currency",
    "payment_format",
    "expected_upstream_account",
    "expected_downstream_account",
    "upstream_gap_minutes",
    "downstream_gap_minutes",
    "required_window_minutes",
]


class EvaluationError(RuntimeError):
    """Raised when the evaluation dataset cannot be produced safely."""


@dataclass(frozen=True)
class PatternTransaction:
    timestamp: datetime
    source_account_key: str
    target_account_key: str
    amount_paid: Decimal
    payment_currency: str
    payment_format: str


@dataclass(frozen=True)
class RandomBlock:
    pattern_id: str
    transactions: tuple[PatternTransaction, ...]


@dataclass
class ParseReport:
    total_random_attempts: int = 0
    usable_random_attempts: int = 0
    malformed_blocks: int = 0
    short_blocks_skipped: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def skipped_blocks(self) -> int:
        return self.malformed_blocks + self.short_blocks_skipped


def normalize_bank_id(bank_id: str) -> str:
    value = bank_id.strip()
    if not value or not value.isdigit():
        raise ValueError(f"invalid bank ID {bank_id!r}")
    return value.lstrip("0") or "0"


def account_key(bank_id: str, account_number: str) -> str:
    account = account_number.strip()
    if not account:
        raise ValueError("account number is empty")
    return f"{normalize_bank_id(bank_id)}|{account}"


def load_patterns_file(s3_client) -> str:
    response = s3_client.get_object(
        Bucket=S3_BUCKET,
        Key=S3_KEY,
    )
    with response["Body"] as body:
        content = body.read()

    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise EvaluationError(
            f"Patterns file is not valid UTF-8: {error}"
        ) from error


def parse_transaction(line: str, line_number: int) -> PatternTransaction:
    try:
        fields = next(csv.reader([line]))
    except csv.Error as error:
        raise ValueError(f"line {line_number}: invalid CSV: {error}") from error

    if len(fields) != 11:
        raise ValueError(
            f"line {line_number}: expected 11 fields, found {len(fields)}"
        )

    (
        timestamp_text,
        from_bank,
        from_account,
        to_bank,
        to_account,
        _amount_received,
        _receiving_currency,
        amount_paid_text,
        payment_currency,
        payment_format,
        _is_laundering,
    ) = (field.strip() for field in fields)

    try:
        timestamp = datetime.strptime(  # noqa: DTZ007
            timestamp_text,
            "%Y/%m/%d %H:%M",
        )
    except ValueError as error:
        raise ValueError(
            f"line {line_number}: invalid timestamp {timestamp_text!r}"
        ) from error

    try:
        amount_paid = Decimal(amount_paid_text)
    except InvalidOperation as error:
        raise ValueError(
            f"line {line_number}: invalid amount_paid {amount_paid_text!r}"
        ) from error

    if not payment_currency:
        raise ValueError(f"line {line_number}: payment currency is empty")
    if not payment_format:
        raise ValueError(f"line {line_number}: payment format is empty")

    return PatternTransaction(
        timestamp=timestamp,
        source_account_key=account_key(from_bank, from_account),
        target_account_key=account_key(to_bank, to_account),
        amount_paid=amount_paid,
        payment_currency=payment_currency,
        payment_format=payment_format,
    )


def validate_chain(transactions: list[PatternTransaction]):
    for position, (previous, current) in enumerate(
        pairwise(transactions),
        start=2,
    ):
        if current.timestamp < previous.timestamp:
            raise ValueError(
                f"transaction {position} occurs before transaction {position - 1}"
            )
        if previous.target_account_key != current.source_account_key:
            raise ValueError(
                f"transactions {position - 1} and {position} do not form a chain"
            )


def parse_random_blocks(text: str) -> tuple[list[RandomBlock], ParseReport]:
    blocks = []
    report = ParseReport()
    current_pattern_id = None
    current_start_line = None
    current_lines = []

    def mark_malformed(reason: str):
        report.malformed_blocks += 1
        report.issues.append(f"{current_pattern_id}: {reason}")

    def finish_block():
        nonlocal current_pattern_id, current_start_line, current_lines

        try:
            transactions = [
                parse_transaction(line, line_number)
                for line_number, line in current_lines
            ]
            validate_chain(transactions)
        except ValueError as error:
            mark_malformed(str(error))
        else:
            if len(transactions) < 3:
                report.short_blocks_skipped += 1
            else:
                blocks.append(
                    RandomBlock(
                        pattern_id=current_pattern_id,
                        transactions=tuple(transactions),
                    )
                )
                report.usable_random_attempts += 1

        current_pattern_id = None
        current_start_line = None
        current_lines = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()

        if line.startswith(RANDOM_HEADER_PREFIX):
            if current_pattern_id is not None:
                mark_malformed(
                    f"block beginning at line {current_start_line} has no end marker"
                )

            report.total_random_attempts += 1
            current_pattern_id = f"RANDOM-{report.total_random_attempts:06d}"
            current_start_line = line_number
            current_lines = []
            continue

        if current_pattern_id is None:
            continue

        if line == RANDOM_END_MARKER:
            finish_block()
            continue

        if line.startswith(ANY_BLOCK_HEADER_PREFIX):
            mark_malformed(
                f"block beginning at line {current_start_line} has no end marker"
            )
            current_pattern_id = None
            current_start_line = None
            current_lines = []
            continue

        if line.startswith(ANY_BLOCK_END_PREFIX):
            mark_malformed(
                f"unexpected end marker {line!r} at line {line_number}"
            )
            current_pattern_id = None
            current_start_line = None
            current_lines = []
            continue

        if line:
            current_lines.append((line_number, line))

    if current_pattern_id is not None:
        mark_malformed(
            f"block beginning at line {current_start_line} reaches end of file"
        )

    return blocks, report


def build_evaluation_cases(blocks: list[RandomBlock]) -> pd.DataFrame:
    rows = []

    for block in blocks:
        transactions = block.transactions

        for index in range(1, len(transactions) - 1):
            predecessor = transactions[index - 1]
            alert = transactions[index]
            successor = transactions[index + 1]

            upstream_gap_minutes = int(
                (alert.timestamp - predecessor.timestamp).total_seconds() / 60
            )
            downstream_gap_minutes = int(
                (successor.timestamp - alert.timestamp).total_seconds() / 60
            )

            rows.append(
                {
                    "pattern_id": block.pattern_id,
                    "pattern_type": "RANDOM",
                    "chain_length_transactions": len(transactions),
                    "alert_position": index + 1,
                    "alert_timestamp": alert.timestamp,
                    "alert_sender": alert.source_account_key,
                    "alert_receiver": alert.target_account_key,
                    "amount_paid": alert.amount_paid,
                    "payment_currency": alert.payment_currency,
                    "payment_format": alert.payment_format,
                    "expected_upstream_account": predecessor.source_account_key,
                    "expected_downstream_account": successor.target_account_key,
                    "upstream_gap_minutes": upstream_gap_minutes,
                    "downstream_gap_minutes": downstream_gap_minutes,
                    "required_window_minutes": max(
                        upstream_gap_minutes,
                        downstream_gap_minutes,
                    ),
                }
            )

    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def print_summary(cases: pd.DataFrame, report: ParseReport):
    required_windows = cases["required_window_minutes"]

    print("BQ2 RANDOM ground-truth evaluation summary")
    print(f"RANDOM laundering attempts: {report.total_random_attempts}")
    print(f"Usable RANDOM attempts: {report.usable_random_attempts}")
    print(f"Total evaluation cases: {len(cases)}")
    print(f"Malformed RANDOM blocks: {report.malformed_blocks}")
    print(f"Too-short RANDOM blocks skipped: {report.short_blocks_skipped}")
    print(f"Total malformed/skipped blocks: {report.skipped_blocks}")
    print(f"Min required window: {required_windows.min():.2f} minutes")
    print(f"Median required window: {required_windows.median():.2f} minutes")
    print(f"Mean required window: {required_windows.mean():.2f} minutes")
    print(f"75th percentile: {required_windows.quantile(0.75):.2f} minutes")
    print(f"90th percentile: {required_windows.quantile(0.90):.2f} minutes")
    print(f"95th percentile: {required_windows.quantile(0.95):.2f} minutes")
    print(f"Maximum required window: {required_windows.max():.2f} minutes")
    print("Ground-truth fixed-window coverage (not actual BQ2 recall):")

    for hours in FIXED_WINDOWS_HOURS:
        covered = int((required_windows <= hours * 60).sum())
        percentage = covered / len(cases) * 100
        print(f"  {hours:>2} hours: {covered}/{len(cases)} ({percentage:.2f}%)")


def main() -> int:
    try:
        session = boto3.Session(region_name=AWS_REGION)
        if session.get_credentials() is None:
            raise NoCredentialsError()

        s3_client = session.client("s3")
        patterns_text = load_patterns_file(s3_client)
        blocks, report = parse_random_blocks(patterns_text)
        cases = build_evaluation_cases(blocks)

        if cases.empty:
            raise EvaluationError("No usable RANDOM evaluation cases were produced")

        cases.to_csv(OUTPUT_PATH, index=False)
        print_summary(cases, report)
        print(f"Saved evaluation cases to: {OUTPUT_PATH}")

        if report.issues:
            print("Malformed block details:", file=sys.stderr)
            for issue in report.issues:
                print(f"  {issue}", file=sys.stderr)

        return 0

    except (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError) as error:
        print(f"ERROR: AWS credentials are unavailable: {error}", file=sys.stderr)
    except ClientError as error:
        print(f"ERROR: Unable to read the S3 Patterns file: {error}", file=sys.stderr)
    except (BotoCoreError, EvaluationError) as error:
        print(f"ERROR: {error}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())
