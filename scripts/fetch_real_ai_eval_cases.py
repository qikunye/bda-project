"""Fetch real CaseContexts for AI summary evaluation from Athena.

Alerts are taken from the BQ1 test-split review queue at fixed queue positions,
selected by rank only. Each case is fetched through the smoke-test path (one
BQ2 and one BQ1 query) and written under the Git-ignored artifacts/aws-smoke/.
Never commit these files.
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.smoke_test_case_context_aws import (
    build_handoff,
    create_real_query_executor,
    execute_smoke_test,
)
from src.config import BQ1_SCORE_TABLE

DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "artifacts" / "aws-smoke" / "ai-eval"
# Positions in the test-split queue, spread across the top 1% (62,504 rows).
DEFAULT_QUEUE_POSITIONS = (1, 2, 10, 100, 500, 1000, 5000, 15000, 35000, 62000)


def build_selection_query(queue_positions: tuple[int, ...]) -> str:
    positions = ", ".join(str(position) for position in queue_positions)
    return f"""
WITH test_queue AS (
    SELECT
        transaction_id,
        source_account_key,
        target_account_key,
        date_format(transaction_timestamp, '%Y-%m-%d %H:%i:%s')
            AS transaction_timestamp,
        ROW_NUMBER() OVER (ORDER BY risk_rank) AS queue_position
    FROM {BQ1_SCORE_TABLE}
    WHERE txn_date BETWEEN DATE '2022-09-14' AND DATE '2022-09-16'
      AND data_split = 'test'
      AND source_account_key <> target_account_key
)
SELECT *
FROM test_queue
WHERE queue_position IN ({positions})
ORDER BY queue_position
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument(
        "--queue-positions",
        type=int,
        nargs="+",
        default=list(DEFAULT_QUEUE_POSITIONS),
    )
    parser.add_argument("--window-hours", type=int, default=48)
    args = parser.parse_args()

    query_executor = create_real_query_executor()
    alerts = query_executor(
        query=build_selection_query(tuple(args.queue_positions))
    )
    print(f"Selected {len(alerts)} alerts from the test queue.")

    for alert in alerts.itertuples(index=False):
        case_name = f"q{int(alert.queue_position):05d}"
        result = execute_smoke_test(
            handoff=build_handoff(
                transaction_id=alert.transaction_id,
                source_account_key=alert.source_account_key,
                target_account_key=alert.target_account_key,
                timestamp=alert.transaction_timestamp,
            ),
            query_executor=query_executor,
            output_directory=args.output_directory / case_name,
            queried_window_hours=args.window_hours,
            with_bq1=True,
            max_candidates=20,
            max_transactions=100,
        )
        statistics = result.context["bq2"]["case_statistics"]
        truncation = result.llm_input["evidence_truncation"]
        print(
            f"{case_name}: {statistics['upstream_connected_account_count']} up / "
            f"{statistics['downstream_connected_account_count']} down accounts, "
            f"{statistics['total_transactions_including_alert']} transactions, "
            f"truncated={truncation['truncated']}, "
            f"bounded JSON {result.bounded_json_size_bytes} bytes"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
