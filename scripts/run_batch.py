"""Writes an evidence brief for each case among a day's top alerts and optionally saves it.
Dry run unless you pass --save. Needs an AWS login and spends money on Athena and Bedrock."""

import argparse
import json
import sys
import time

import boto3

from src import config
from src.agent.batch import BatchIncomplete, publish, run_day
from src.agent.data import AgentData, athena_runner
from src.agent.llm import SpendGuard, create_agent_llm
from src.agent.report import fallback_reason, print_summary, summarise_runs
from src.agent.store import DynamoStore, S3TraceStore


def check_tables_exist() -> None:
    """Fail before any LLM money is spent if a table is missing."""

    client = boto3.client("dynamodb", region_name=config.AWS_REGION)
    for name in (config.AGENT_DECISIONS_TABLE, config.AGENT_CASES_TABLE):
        try:
            client.describe_table(TableName=name)
        except client.exceptions.ResourceNotFoundException:
            raise SystemExit(
                f"Table {name} does not exist. "
                "Run python -m scripts.create_agent_cases_table first."
            )


def llm_failures(runs) -> list[str]:
    """Case IDs whose brief is a fallback because the LLM could not be used at all (a failed call, such
    as a daily token quota, or the spend cap), not because the model wrote an invalid brief."""

    bad = []
    for case, result in runs:
        if result.get("status") != "ok" or not result["stats"]["fallback"]:
            continue
        reason = fallback_reason(result) or ""
        if reason.startswith("LLM call failed") or "spend" in reason.lower():
            bad.append(case.case_id)
    return bad


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True, choices=config.AGENT_SCORED_DATES)
    parser.add_argument("--top-n", type=int, default=config.AGENT_BATCH_TOP_N)
    parser.add_argument(
        "--concurrency", type=int, default=config.AGENT_BATCH_CONCURRENCY
    )
    parser.add_argument("--model", default=config.AGENT_MODEL_ID)
    parser.add_argument("--spend-cap", type=float, default=config.AGENT_SPEND_CAP_USD)
    parser.add_argument("--run-id", help="Default: batch-<day>-<UTC time>.")
    parser.add_argument(
        "--out", help="Also save the full result to this local JSON file."
    )
    parser.add_argument(
        "--allow-llm-failures",
        action="store_true",
        help="Save even if some cases fell back because the LLM call failed or the spend cap was reached.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Write to DynamoDB and S3. Without it, nothing is written.",
    )
    args = parser.parse_args()

    if args.save:
        check_tables_exist()

    started = time.monotonic()
    data = AgentData(athena_runner())
    spend = SpendGuard(args.spend_cap)

    def progress(done, total, case, outcome):
        label = (
            f"ERROR {outcome!r}"
            if isinstance(outcome, Exception)
            else f"{len(outcome['findings'])} findings"
            if outcome["status"] == "ok"
            else outcome["status"]
        )
        print(
            f"[{done}/{total}] {case.case_id}: {label} (spend US${spend.spent_usd:.2f})"
        )

    output = run_day(
        data,
        create_agent_llm(args.model),
        args.day,
        top_n=args.top_n,
        concurrency=args.concurrency,
        spend=spend,
        run_id=args.run_id,
        on_case_done=progress,
    )

    summary = summarise_runs(
        [r for _, r in output.runs], output.failures, spend.spent_usd
    )
    print_summary(summary, data.bytes_scanned / 1024**3, time.monotonic() - started)
    print(
        f"Run {output.run_id}: {len(output.decision_rows)} decision rows, {len(output.case_rows)} cases."
    )
    for failure in output.failures:
        print("FAILED:", failure)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "day": output.day,
                    "run_id": output.run_id,
                    "summary": summary,
                    "failures": output.failures,
                    "decision_rows": output.decision_rows,
                    "case_rows": output.case_rows,
                    "results": [
                        {"case_id": case.case_id, "result": result}
                        for case, result in output.runs
                    ],
                },
                handle,
                indent=2,
                sort_keys=True,
                default=str,
            )
        print("Saved locally:", args.out)

    if args.save and not args.allow_llm_failures:
        bad = llm_failures(output.runs)
        if bad:
            print(
                f"NOT SAVED: {len(bad)} cases fell back because the LLM call failed or the spend cap was "
                "reached (for example a Bedrock daily token quota). Rerun later, or pass --allow-llm-failures."
            )
            return 1

    if not args.save:
        print("Dry run: nothing was written to DynamoDB or S3. Add --save to write.")
        return 1 if output.failures else 0

    try:
        report = publish(output, DynamoStore(), S3TraceStore())
    except BatchIncomplete as error:
        print("NOT SAVED:", error)
        return 1
    print("Saved:", report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
