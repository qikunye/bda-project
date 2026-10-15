"""Opt-in real-AWS smoke test for the validated CaseContext data layer."""

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bq1.queries import build_bq1_score_lookup_query
from src.bq1.service import BQ1MetadataError
from src.bq2.evidence import EvidenceError
from src.bq2.queries import build_case_edges_query
from src.config import (
    ATHENA_DATABASE,
    AWS_REGION,
    BQ1_SCORE_TABLE,
    BQ2_ATHENA_OUTPUT,
    BQ2_EDGE_TABLE,
)
from src.context.projection import build_llm_input
from src.context.service import CaseContextServiceError, load_case_context
from src.context.validation import (
    CaseContextError,
    serialize_case_context,
    validate_case_context,
    validate_llm_input,
)
from src.shared.contracts import AlertHandoff

DEFAULT_OUTPUT_DIRECTORY = Path("artifacts/aws-smoke")
FULL_CONTEXT_FILENAME = "case_context.real.json"
LLM_INPUT_FILENAME = "llm_input.real.json"
_FORBIDDEN_OUTPUT_TERMS = (
    "evaluation_is_laundering",
    "data_split",
    "laundering_pattern",
    "pattern_type",
)

QueryExecutor = Callable[..., Any]


class SmokeTestError(RuntimeError):
    """Raised when smoke-test planning or execution violates a safety check."""


@dataclass(frozen=True)
class QueryPlan:
    """Validated SQL plan produced without contacting AWS."""

    bq2_query: str
    bq1_query: str | None
    queried_window_hours: int

    @property
    def bq2_query_count(self) -> int:
        return 1

    @property
    def bq1_query_count(self) -> int:
        return int(self.bq1_query is not None)


@dataclass(frozen=True)
class SmokeTestResult:
    """Validated outputs and non-sensitive execution measurements."""

    context: dict[str, Any]
    llm_input: dict[str, Any]
    full_context_path: Path
    llm_input_path: Path
    full_json_size_bytes: int
    bounded_json_size_bytes: int
    bq2_query_count: int
    bq1_query_count: int


class PlannedQueryExecutor:
    """Allow only the one BQ2 and optional one BQ1 query in a plan."""

    def __init__(self, delegate: QueryExecutor, plan: QueryPlan):
        if not callable(delegate):
            raise TypeError("query_executor must be callable.")
        self._delegate = delegate
        self._plan = plan
        self.bq2_query_count = 0
        self.bq1_query_count = 0

    def __call__(self, *, query: str):
        if query == self._plan.bq2_query:
            if self.bq2_query_count:
                raise SmokeTestError("Refusing a duplicate BQ2 query.")
            self.bq2_query_count += 1
        elif self._plan.bq1_query is not None and query == self._plan.bq1_query:
            if self.bq1_query_count:
                raise SmokeTestError("Refusing a duplicate BQ1 query.")
            self.bq1_query_count += 1
        else:
            raise SmokeTestError("Refusing an unexpected Athena query.")
        return self._delegate(query=query)


def positive_int(value: str) -> int:
    """Parse a positive CLI integer."""

    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def non_negative_int(value: str) -> int:
    """Parse a non-negative CLI integer."""

    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def parse_alert_timestamp(value: str) -> datetime:
    """Parse the repository's minute-resolution UTC timestamp convention."""

    if not isinstance(value, str) or value != value.strip():
        raise ValueError("timestamp must use YYYY-MM-DD HH:MM:SS in UTC.")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise ValueError(
            "timestamp must use YYYY-MM-DD HH:MM:SS in UTC."
        ) from error
    if parsed.second != 0:
        raise ValueError("timestamp must follow minute-resolution source data.")
    return parsed


def build_handoff(
    *,
    transaction_id: str,
    source_account_key: str,
    target_account_key: str,
    timestamp: str,
) -> AlertHandoff:
    """Build a four-field handoff without substituting default alert values."""

    for field_name, value in (
        ("transaction_id", transaction_id),
        ("source_account_key", source_account_key),
        ("target_account_key", target_account_key),
    ):
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{field_name} must be a non-empty exact value.")
    return AlertHandoff(
        transaction_id=transaction_id,
        source_account_key=source_account_key,
        target_account_key=target_account_key,
        transaction_timestamp=parse_alert_timestamp(timestamp),
    )


def _validate_query_plan(plan: QueryPlan) -> None:
    if plan.bq2_query.count("g.txn_date") != 3:
        raise SmokeTestError(
            "BQ2 query does not contain all expected txn_date predicates."
        )
    if plan.bq2_query.count("g.txn_date BETWEEN") != 2:
        raise SmokeTestError(
            "BQ2 query does not contain both bounded date ranges."
        )
    expected_minutes = plan.queried_window_hours * 60
    if f"{expected_minutes}\n                AS window_minutes" not in (
        plan.bq2_query
    ):
        raise SmokeTestError("BQ2 query window does not match the requested horizon.")
    if plan.bq1_query is not None and "txn_date = DATE" not in plan.bq1_query:
        raise SmokeTestError("BQ1 query is missing its txn_date predicate.")


def build_query_plan(
    *,
    handoff: AlertHandoff,
    queried_window_hours: int,
    with_bq1: bool,
) -> QueryPlan:
    """Construct and inspect the exact SQL without creating an AWS client."""

    if not isinstance(handoff, AlertHandoff):
        raise TypeError("handoff must be an AlertHandoff.")
    if (
        not isinstance(queried_window_hours, int)
        or isinstance(queried_window_hours, bool)
        or queried_window_hours <= 0
    ):
        raise ValueError("queried_window_hours must be a positive integer.")
    if not isinstance(with_bq1, bool):
        raise TypeError("with_bq1 must be a boolean.")
    timestamp_value = handoff.transaction_timestamp
    if not isinstance(timestamp_value, datetime):
        raise TypeError("handoff.transaction_timestamp must be a datetime.")
    if timestamp_value.tzinfo is None:
        timestamp_value = timestamp_value.replace(tzinfo=timezone.utc)
    else:
        timestamp_value = timestamp_value.astimezone(timezone.utc)
    if timestamp_value.second != 0 or timestamp_value.microsecond != 0:
        raise ValueError("Alert timestamp must use minute-resolution source data.")
    timestamp = timestamp_value.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    plan = QueryPlan(
        bq2_query=build_case_edges_query(
            handoff.source_account_key,
            handoff.target_account_key,
            timestamp,
            handoff.transaction_id,
            queried_window_hours * 60,
            table_name=BQ2_EDGE_TABLE,
        ),
        bq1_query=(
            build_bq1_score_lookup_query(
                handoff.transaction_id,
                timestamp,
                table_name=BQ1_SCORE_TABLE,
            )
            if with_bq1
            else None
        ),
        queried_window_hours=queried_window_hours,
    )
    _validate_query_plan(plan)
    return plan


def _assert_safe_serialized_output(serialized: str) -> None:
    matches = [term for term in _FORBIDDEN_OUTPUT_TERMS if term in serialized]
    if matches:
        raise SmokeTestError(
            "Ground-truth material appeared in output: " + ", ".join(matches)
        )


def execute_smoke_test(
    *,
    handoff: AlertHandoff,
    query_executor: QueryExecutor,
    output_directory: Path,
    queried_window_hours: int,
    with_bq1: bool,
    max_candidates: int,
    max_transactions: int,
) -> SmokeTestResult:
    """Execute an injected query plan and write two locally validated JSON files."""

    plan = build_query_plan(
        handoff=handoff,
        queried_window_hours=queried_window_hours,
        with_bq1=with_bq1,
    )
    planned_executor = PlannedQueryExecutor(query_executor, plan)
    context = load_case_context(
        handoff=handoff,
        query_executor=planned_executor,
        fetch_bq1=with_bq1,
        queried_window_hours=queried_window_hours,
    )
    validate_case_context(context)
    llm_input = build_llm_input(
        context,
        max_candidates=max_candidates,
        max_transactions=max_transactions,
    )
    validate_llm_input(llm_input)

    if context["provenance"]["queried_window_hours"] != queried_window_hours:
        raise SmokeTestError("CaseContext horizon does not match the request.")
    full_json = serialize_case_context(context)
    bounded_json = serialize_case_context(llm_input)
    _assert_safe_serialized_output(full_json)
    _assert_safe_serialized_output(bounded_json)

    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    full_context_path = output_directory / FULL_CONTEXT_FILENAME
    llm_input_path = output_directory / LLM_INPUT_FILENAME
    full_context_path.write_text(full_json + "\n", encoding="utf-8")
    llm_input_path.write_text(bounded_json + "\n", encoding="utf-8")

    return SmokeTestResult(
        context=context,
        llm_input=llm_input,
        full_context_path=full_context_path,
        llm_input_path=llm_input_path,
        full_json_size_bytes=full_context_path.stat().st_size,
        bounded_json_size_bytes=llm_input_path.stat().st_size,
        bq2_query_count=planned_executor.bq2_query_count,
        bq1_query_count=planned_executor.bq1_query_count,
    )


def create_real_query_executor() -> QueryExecutor:
    """Create one reusable Athena client through the standard provider chain."""

    from src.aws.athena import create_athena_client, execute_query

    client = create_athena_client(AWS_REGION)
    return partial(
        execute_query,
        client,
        database=ATHENA_DATABASE,
        output_location=BQ2_ATHENA_OUTPUT,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan a CaseContext Athena smoke test, or execute it only with "
            "the explicit --execute flag."
        )
    )
    parser.add_argument("--transaction-id", required=True)
    parser.add_argument("--source-account-key", required=True)
    parser.add_argument("--target-account-key", required=True)
    parser.add_argument(
        "--timestamp",
        required=True,
        help="UTC timestamp in YYYY-MM-DD HH:MM:SS format",
    )
    parser.add_argument("--window-hours", type=positive_int, default=48)
    parser.add_argument("--with-bq1", action="store_true")
    parser.add_argument("--max-candidates", type=non_negative_int, default=20)
    parser.add_argument("--max-transactions", type=positive_int, default=100)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Opt in to real Athena queries and local JSON output",
    )
    return parser


def _print_dry_run(plan: QueryPlan) -> None:
    print("CaseContext AWS Smoke Test")
    print()
    print("Execution mode: DRY RUN")
    print("AWS client created: no")
    print("AWS API operations: 0")
    print(f"Investigation window: {plan.queried_window_hours}h")
    print(f"Planned BQ2 query count: {plan.bq2_query_count}")
    print(f"Planned BQ1 query count: {plan.bq1_query_count}")
    print("Partition-filter validation: PASS")
    print("Athena scan telemetry: unavailable from the current executor")
    print()
    print("Intended validated outputs after explicit --execute:")
    print(f"- {DEFAULT_OUTPUT_DIRECTORY / FULL_CONTEXT_FILENAME}")
    print(f"- {DEFAULT_OUTPUT_DIRECTORY / LLM_INPUT_FILENAME}")


def _print_execution_summary(result: SmokeTestResult, window_hours: int) -> None:
    statistics = result.context["bq2"]["case_statistics"]
    print("CaseContext AWS Smoke Test")
    print()
    print("Execution mode: REAL AWS")
    print(f"Investigation window: {window_hours}h")
    print(f"BQ2 query count: {result.bq2_query_count}")
    print(f"BQ1 query count: {result.bq1_query_count}")
    print(f"BQ1 status: {result.context['bq1']['status']}")
    print(
        "Transactions returned: "
        f"{statistics['total_transactions_including_alert']}"
    )
    print(f"Connected accounts: {statistics['unique_connected_account_count']}")
    print(f"Ranked candidates: {len(result.context['bq2']['candidates'])}")
    print("CaseContext validation: PASS")
    print("LLM projection validation: PASS")
    print(f"Full JSON size: {result.full_json_size_bytes / 1024:.2f} KB")
    print(f"Bounded JSON size: {result.bounded_json_size_bytes / 1024:.2f} KB")
    print("Athena query IDs and scan metrics: unavailable from current executor")
    print("Estimated scan cost: unavailable; no unverified estimate reported")
    print()
    print("Output files:")
    print(result.full_context_path)
    print(result.llm_input_path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        handoff = build_handoff(
            transaction_id=args.transaction_id,
            source_account_key=args.source_account_key,
            target_account_key=args.target_account_key,
            timestamp=args.timestamp,
        )
        plan = build_query_plan(
            handoff=handoff,
            queried_window_hours=args.window_hours,
            with_bq1=args.with_bq1,
        )
        if not args.execute:
            _print_dry_run(plan)
            return 0

        result = execute_smoke_test(
            handoff=handoff,
            query_executor=create_real_query_executor(),
            output_directory=DEFAULT_OUTPUT_DIRECTORY,
            queried_window_hours=args.window_hours,
            with_bq1=args.with_bq1,
            max_candidates=args.max_candidates,
            max_transactions=args.max_transactions,
        )
        _print_execution_summary(result, args.window_hours)
        return 0
    except (
        BotoCoreError,
        ClientError,
        BQ1MetadataError,
        CaseContextError,
        CaseContextServiceError,
        EvidenceError,
        OSError,
        RuntimeError,
        SmokeTestError,
        TypeError,
        ValueError,
    ) as error:
        print(
            f"CaseContext AWS smoke test failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
