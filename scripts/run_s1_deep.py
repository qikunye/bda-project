"""Runs the code-only brief (S1) on a deeper slice of one day's queue, several cases at a time.

As described in docs/evaluation.md, the top 100 lacks negative cases for per-finding rates, so
S1 runs on the top 500 (or more) while S2 stays on the top 100. Same rules, same end-of-day cutoff
and same output shape as run_s1, plus each case's BQ1 rank so the evaluation can band by rank.
Reads no labels and calls no LLM; spends only on Athena (about 100 GB, US$0.5, for a top 500).

    .venv/bin/python -m scripts.run_s1_deep --day 2022-09-14 --top-n 500 --out runs/s1deep_2022-09-14.json
"""

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

from src import config
from src.agent import tools
from src.agent.baseline import Settings, brief_case, end_of_day
from src.agent.cases import group_into_cases, queue_alerts_from_frame
from src.agent.data import AgentData, athena_runner


def run_day(
    data: AgentData, day: str, top_n: int, concurrency: int, on_done=None
) -> dict:
    settings = replace(Settings(), as_of=end_of_day(day))
    frame = tools.load_day_queue(data, day, top_n)
    rank_of = dict(zip(frame["transaction_id"], frame["risk_rank_day"]))
    cases = group_into_cases(
        queue_alerts_from_frame(frame), day, window_h=settings.window_h
    )

    results: dict[str, dict] = {}
    failures: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(brief_case, data, case, settings): case for case in cases
        }
        for done, future in enumerate(as_completed(futures), 1):
            case = futures[future]
            try:
                results[case.case_id] = future.result()
            except Exception as error:  # noqa: BLE001 - one bad case must not lose the others.
                failures.append({"case_id": case.case_id, "error": repr(error)})
            if on_done:
                on_done(done, len(cases), case, results.get(case.case_id))

    return {
        "day": day,
        "top_n": top_n,
        "failures": failures,
        "cases": [
            {
                "case_id": case.case_id,
                "representative_txn_id": case.representative_txn_id,
                "representative_rank_day": int(rank_of[case.representative_txn_id]),
                "member_txn_ids": list(case.member_txn_ids),
                "summary": results[case.case_id]["summary"],
                "findings": results[case.case_id]["findings"],
                "gaps": results[case.case_id]["gaps"],
                "next_checks": results[case.case_id]["next_checks"],
            }
            for case in cases  # Queue order, best rank first, as run_s1.
            if case.case_id in results
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--day", required=True, choices=config.AGENT_SCORED_DATES)
    parser.add_argument("--top-n", type=int, default=500)
    parser.add_argument(
        "--concurrency", type=int, default=config.AGENT_BATCH_CONCURRENCY
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    started = time.monotonic()
    data = AgentData(athena_runner())

    def progress(done, total, case, result):
        if done % 25 == 0 or done == total:
            print(
                f"[{done}/{total}] {(time.monotonic() - started) / 60:.1f} min",
                flush=True,
            )

    output = run_day(data, args.day, args.top_n, args.concurrency, progress)
    cases = output["cases"]
    print(
        f"Day {args.day}: top {args.top_n} -> {len(cases)} cases, {len(output['failures'])} failed"
    )
    print(
        "Findings:",
        dict(Counter(f["code"] for c in cases for f in c["findings"]).most_common()),
    )
    gb = data.bytes_scanned / 1024**3
    print(
        f"Athena: {gb:.2f} GB (about US${gb / 1024 * 5:.3f}), {(time.monotonic() - started) / 60:.1f} min"
    )
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2, sort_keys=True, default=str)
    print("Saved:", args.out)
    return 1 if output["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
