"""Compare summary prompt versions on synthetic cases with Amazon Bedrock.

This makes real, billed Bedrock calls: up to two per case and prompt version.
Only synthetic cases are used, so the output CSV is safe to commit.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ai.evaluation import evaluate_summary
from src.ai.llm_client import BedrockConverseClient, create_bedrock_runtime_client
from src.ai.prompts import SUMMARY_PROMPTS
from src.ai.summary import generate_case_summary
from src.bq1.contracts import BQ1MetadataResult
from src.config import AWS_REGION, BEDROCK_MODEL_ID
from src.context.builder import build_case_context
from src.context.projection import build_llm_input
from tests.unit.context_fixtures import (
    make_bq1_result,
    make_handoff,
    make_raw_edges,
)

DEFAULT_OUTPUT = PROJECT_ROOT / "evaluations" / "ai" / "summary_prompt_comparison.csv"
EXAMPLE_INPUT = (
    PROJECT_ROOT / "docs" / "handovers" / "examples" / "llm_input.synthetic.json"
)


def _context(raw_edges=None, bq1_result=None):
    return build_case_context(
        handoff=make_handoff(),
        raw_edges=make_raw_edges() if raw_edges is None else raw_edges,
        bq1_result=make_bq1_result() if bq1_result is None else bq1_result,
        queried_window_hours=48,
    )


def _clean_single_currency_edges():
    edges = make_raw_edges()
    edges = edges[
        ~edges["transaction_id"].isin(["txn_synthetic_u3", "txn_synthetic_d2"])
    ].copy()
    edges["payment_currency"] = "USD"
    return edges


def synthetic_cases() -> dict[str, dict]:
    return {
        "full": build_llm_input(_context()),
        "truncated": build_llm_input(
            _context(), max_candidates=2, max_transactions=3
        ),
        "bq1_unavailable": build_llm_input(
            _context(bq1_result=BQ1MetadataResult.unavailable("Not loaded."))
        ),
        "bq1_not_found": build_llm_input(
            _context(bq1_result=BQ1MetadataResult.not_found("No BQ1 row."))
        ),
        "alert_only": build_llm_input(_context(raw_edges=make_raw_edges().iloc[:1])),
        "clean_single_currency": build_llm_input(
            _context(raw_edges=_clean_single_currency_edges())
        ),
        "docs_example": json.loads(EXAMPLE_INPUT.read_text(encoding="utf-8")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompts",
        nargs="+",
        default=sorted(SUMMARY_PROMPTS),
        choices=sorted(SUMMARY_PROMPTS),
    )
    parser.add_argument("--model-id", default=BEDROCK_MODEL_ID)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    client = BedrockConverseClient(
        create_bedrock_runtime_client(AWS_REGION),
        model_id=args.model_id,
    )
    rows = []
    for case_name, llm_input in synthetic_cases().items():
        for version in args.prompts:
            result = generate_case_summary(
                llm_input,
                client,
                prompt=SUMMARY_PROMPTS[version],
            )
            quality = (
                evaluate_summary(result.response, llm_input)
                if result.response is not None
                else None
            )
            rows.append(
                {
                    "case": case_name,
                    "prompt_version": version,
                    "model_id": result.model_id,
                    "status": result.status,
                    "attempts": len(result.attempts),
                    "grounding_issues": "; ".join(
                        f"attempt {number}: "
                        + ",".join(issue.code for issue in attempt.issues)
                        for number, attempt in enumerate(result.attempts, 1)
                        if attempt.issues
                    ),
                    "quality_passed": "" if quality is None else quality.passed,
                    "failed_checks": (
                        "" if quality is None else ",".join(quality.failed_checks)
                    ),
                    "sentence_count": "" if quality is None else quality.sentence_count,
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "latency_ms": sum(
                        attempt.completion.latency_ms or 0
                        for attempt in result.attempts
                    ),
                    "answer": (
                        "" if result.response is None else result.response.answer
                    ),
                }
            )
            row = rows[-1]
            print(
                f"{case_name:22} {version:11} {row['status']:9} "
                f"attempts={row['attempts']} quality={row['quality_passed']} "
                f"failed=[{row['failed_checks']}] issues=[{row['grounding_issues']}]"
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    for version in args.prompts:
        version_rows = [row for row in rows if row["prompt_version"] == version]
        print(
            f"\n{version}: grounded {sum(r['status'] == 'grounded' for r in version_rows)}"
            f"/{len(version_rows)}, quality passed "
            f"{sum(r['quality_passed'] is True for r in version_rows)}"
            f"/{len(version_rows)}, first-attempt grounded "
            f"{sum(r['attempts'] == 1 and r['status'] == 'grounded' for r in version_rows)}"
            f"/{len(version_rows)}, tokens in/out "
            f"{sum(r['input_tokens'] for r in version_rows)}"
            f"/{sum(r['output_tokens'] for r in version_rows)}"
        )
    print(f"\nWrote {args.output.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
