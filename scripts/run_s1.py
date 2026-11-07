"""Run the deterministic baseline (S1) for one day against real Athena (PLAN.md milestone 2).

Usage (needs an AWS login for the g1team5 account):
    python -m scripts.run_s1 --day 2022-09-15 --out /path/to/s1_20220915.json
    python -m scripts.run_s1 --day 2022-09-15 --out /path/to/s1_b.json --compare-to /path/to/s1.json

--compare-to runs the day and fails if its cases, decisions or review list differ from an earlier
saved run, ignoring only timing. It reads no labels, and writes nothing to AWS except Athena's
result files.
"""

import argparse
import json
import sys
import time
from collections import Counter

from src import config
from src.agent import tools
from src.agent.baseline import triage_day
from src.agent.data import AgentData, athena_runner
from src.agent.decisions import build_decision_rows
from src.agent.review_list import build_review_list


def run_day(day: str, top_n: int, run_id: str, created_at: str) -> tuple[dict, AgentData]:
    data = AgentData(athena_runner())
    results = triage_day(data, day, top_n=top_n)

    rows = []
    for case, result in results:
        rows.extend(
            build_decision_rows(
                result,
                run_id=run_id,
                created_at=created_at,
                member_txn_ids=case.member_txn_ids,
            )
        )

    queue = tools.load_day_queue(data, day, top_n)["transaction_id"].tolist()
    output = {
        "day": day,
        "top_n": top_n,
        "cases": [
            {
                "case_id": case.case_id,
                "representative_txn_id": case.representative_txn_id,
                "member_txn_ids": list(case.member_txn_ids),
                "decision": result["decision"],
                "rendered_claims": result["rendered_claims"],
            }
            for case, result in results
        ],
        "rows": rows,
        "review_list": build_review_list(queue, rows),
    }
    return output, data


def summarise(output: dict, data: AgentData, seconds: float) -> None:
    cases = output["cases"]
    sizes = Counter(len(c["member_txn_ids"]) for c in cases)
    print(f"Day {output['day']}: {output['top_n']} alerts -> {len(cases)} cases")
    print("Case sizes:", dict(sorted(sizes.items())))
    print("Verdicts:", dict(Counter(c["decision"]["verdict"] for c in cases).most_common()))
    print("Confidence:", dict(Counter(c["decision"]["confidence"] for c in cases).most_common()))
    print("Claims:", dict(Counter(
        claim["code"] for c in cases for claim in c["decision"]["claims"]
    ).most_common()))
    scanned_gb = data.bytes_scanned / 1024**3
    print(
        f"Athena: {scanned_gb:.2f} GB scanned (about US${scanned_gb / 1024 * 5:.3f}), "
        f"{seconds / 60:.1f} min"
    )


def comparable(output: dict) -> dict:
    """The parts of a run that must repeat. Timing changes on every run, so it is left out."""

    return json.loads(
        json.dumps(
            {
                "cases": output["cases"],
                "rows": [
                    {k: v for k, v in row.items() if k != "latency_ms"}
                    for row in output["rows"]
                ],
                "review_list": output["review_list"],
            }
        )
    )


def first_difference(a, b, path: str = "") -> str | None:
    """Where two comparable runs first differ, or None if they match."""

    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}.{key}: only in one run"
            found = first_difference(a[key], b[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: {len(a)} items vs {len(b)} items"
        for index, (left, right) in enumerate(zip(a, b)):
            found = first_difference(left, right, f"{path}[{index}]")
            if found:
                return found
        return None
    return None if a == b else f"{path}: {a!r} vs {b!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True, choices=config.AGENT_SCORED_DATES)
    parser.add_argument("--top-n", type=int, default=config.AGENT_BATCH_TOP_N)
    parser.add_argument("--out", required=True)
    parser.add_argument("--compare-to", help="An earlier saved run of the same day.")
    args = parser.parse_args()

    earlier = None
    if args.compare_to:
        with open(args.compare_to, encoding="utf-8") as handle:
            earlier = json.load(handle)

    run_id = f"s1-{args.day}"
    created_at = f"{args.day}T00:00:00+00:00"  # Fixed, so repeated runs can be compared.

    started = time.monotonic()
    output, data = run_day(args.day, args.top_n, run_id, created_at)
    summarise(output, data, time.monotonic() - started)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2, sort_keys=True)
    print("Saved:", args.out)

    if earlier is not None:
        difference = first_difference(comparable(earlier), comparable(output))
        if difference:
            print("FAILED: first difference at", difference)
            return 1
        print("PASSED: both runs gave identical cases, decisions and review list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
