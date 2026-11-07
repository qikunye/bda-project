"""Evaluates a day's saved S1 and S2 briefs against the labels and the patterns file.
Reads labels, so run it only after the thresholds and prompt are frozen in a commit.
Needs an AWS login. Spends a few cents on Athena."""

import argparse
import csv
import json
import sys
from pathlib import Path

import boto3

from src import config
from src.agent.data import athena_runner
from src.evaluation import manual_review, metrics, patterns
from src.evaluation.labels import fetch_labels
from src.evaluation.runs import load_run

PATTERNS_BUCKET = "is459-aml-g1t5"
PATTERNS_KEY = "bronze/ibm-aml/li-medium/patterns/LI-Medium_Patterns.txt"


def band_by_position(cases: list) -> dict[str, str]:
    """Cases come best rank first, so a third of the list each gives three BQ1 rank bands."""

    third = max(1, -(-len(cases) // 3))
    return {c.case_id: ("top" if i < third else "middle" if i < 2 * third else "bottom") for i, c in enumerate(cases)}


def write_sheet(sheet: list[dict], path: str) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(sheet[0]) if sheet else ["case_id"])
        writer.writeheader()
        writer.writerows(sheet)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True, choices=config.AGENT_SCORED_DATES)
    parser.add_argument("--s1", required=True, help="run_s1 --out file for this day.")
    parser.add_argument("--s2", required=True, help="run_batch --out file for this day.")
    parser.add_argument("--out", help="Where to write the report. Required unless --review-only.")
    parser.add_argument(
        "--review-only",
        action="store_true",
        help="Write the review sheet (--review-csv) and stop. Reads no labels, so do it first.",
    )
    parser.add_argument("--review-csv", help="Write the sheet for reading observations by hand.")
    parser.add_argument("--marked-csv", help="A filled-in review sheet to tally.")
    parser.add_argument(
        "--confirm-frozen",
        action="store_true",
        help="Required: confirms the thresholds and prompt are frozen in a commit.",
    )
    args = parser.parse_args()
    if args.review_only:
        if not args.review_csv:
            raise SystemExit("--review-only needs --review-csv.")
        write_sheet(manual_review.review_rows(load_run(args.s2)), args.review_csv)
        print(f"Wrote {args.review_csv}. Mark each row useful, obvious or wrong, then run without --review-only.")
        return 0
    if not args.out:
        raise SystemExit("--out is required.")
    if not args.confirm_frozen:
        raise SystemExit("Refusing to read labels: pass --confirm-frozen once the freeze is committed.")

    s1, s2 = load_run(args.s1), load_run(args.s2)
    report: dict = {"day": args.day, "reliability_s2": metrics.reliability(s2)}
    report["s2_vs_s1"] = metrics.compare_runs(s1, s2)

    if args.review_csv:
        write_sheet(manual_review.review_rows(s2), args.review_csv)
    if args.marked_csv:
        with open(args.marked_csv, newline="") as handle:
            report["observation_marks"] = manual_review.tally(list(csv.DictReader(handle)))

    all_ids = sorted({t for c in (*s1, *s2) for t in c.member_txn_ids})
    labels, scored = fetch_labels(athena_runner(), all_ids)
    report["labels_found"] = {"alerts": len(all_ids), "with_label": len(labels), "positives": sum(labels.values())}
    report["s1_code_rates"] = metrics.code_rates(s1, labels)
    report["s2_code_rates"] = metrics.code_rates(s2, labels)

    report["s1_bq1_derived_by_rank_band"] = metrics.banded_code_rates(s1, labels, band_by_position(s1))

    text = boto3.client("s3", region_name=config.AWS_REGION).get_object(
        Bucket=PATTERNS_BUCKET, Key=PATTERNS_KEY
    )["Body"].read().decode("utf-8-sig")
    txn_type, coverage = patterns.transaction_types(patterns.parse_attempts(text), scored)
    members = {c.case_id: c.member_txn_ids for c in s1}
    report["pattern_coverage"] = coverage
    report["s1_pattern_match"] = metrics.pattern_match(s1, patterns.case_types(members, txn_type))
    report["s2_pattern_match"] = metrics.pattern_match(s2, patterns.case_types(members, txn_type))

    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
