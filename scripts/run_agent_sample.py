"""Runs the LLM agent on the first few cases of a day and reports valid, repair and fallback rates and tokens.
Needs an AWS login and spends money on Athena and Bedrock. Example: python -m scripts.run_agent_sample --day 2022-09-14 --limit 20 --out sample.json
"""

import argparse
import json
import sys
import time

from src import config
from src.agent import tools
from src.agent.baseline import Settings, end_of_day
from src.agent.cases import group_into_cases, queue_alerts_from_frame
from src.agent.data import AgentData, athena_runner
from src.agent.llm import SpendGuard, create_agent_llm
from src.agent.loop import LoopSettings, run_agent
from src.agent.report import (  # noqa: F401 - re-exported for the tests
    fallback_reason,
    print_summary,
    rejection_reasons,
    summarise_runs,
)
from src.agent.sql import LabelLeakError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True, choices=config.AGENT_SCORED_DATES)
    parser.add_argument(
        "--limit", type=int, default=20, help="Cases to run, best rank first."
    )
    parser.add_argument("--top-n", type=int, default=config.AGENT_BATCH_TOP_N)
    parser.add_argument("--model", default=config.AGENT_MODEL_ID)
    parser.add_argument("--spend-cap", type=float, default=config.AGENT_SPEND_CAP_USD)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    started = time.monotonic()
    data = AgentData(athena_runner())
    frame = tools.load_day_queue(data, args.day, args.top_n)
    cases = group_into_cases(queue_alerts_from_frame(frame), args.day)[: args.limit]
    llm = create_agent_llm(args.model)
    spend = SpendGuard(args.spend_cap)
    # The same end-of-day view as the batch, so the sample predicts it.
    settings = LoopSettings(s1=Settings(as_of=end_of_day(args.day)))

    done, failures = [], []
    for index, case in enumerate(cases, start=1):
        try:
            result = run_agent(data, llm, case, settings=settings, spend=spend)
        except LabelLeakError:
            raise  # Rule 2: never carry on after a label reached the pipeline.
        except Exception as error:  # noqa: BLE001 - report the case and go on.
            failures.append(
                {
                    "representative_txn_id": case.representative_txn_id,
                    "error": repr(error),
                }
            )
            print(f"[{index}/{len(cases)}] {case.case_id}: ERROR {error!r}")
            continue
        done.append((case, result))
        stats = result["stats"]
        print(
            f"[{index}/{len(cases)}] {case.case_id}: "
            f"{result['decision']['verdict'] if result['decision'] else result['status']} "
            f"{stats['validation'] if stats else ''} "
            f"steps={stats['steps'] if stats else '-'} "
            f"tokens={stats['tokens_in'] + stats['tokens_out'] if stats else '-'} "
            f"spend=US${spend.spent_usd:.3f}"
        )

    summary = summarise_runs([r for _, r in done], failures, spend.spent_usd)
    output = {
        "day": args.day,
        "model": args.model,
        "summary": summary,
        "failures": failures,
        "cases": [
            {
                "case_id": case.case_id,
                "member_txn_ids": list(case.member_txn_ids),
                "result": result,
            }
            for case, result in done
        ],
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2, sort_keys=True, default=str)
    print_summary(summary, data.bytes_scanned / 1024**3, time.monotonic() - started)
    print("Saved:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
