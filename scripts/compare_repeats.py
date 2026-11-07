"""Compares several S2 runs of the same alerts (PLAN.md section 13: "run a 50-alert subset 3 times").
Reads no labels, so it can be done before the freeze is read against labels.

Make the runs first (dry runs, nothing is saved; about US$5 each for the top 50):
    .venv/bin/python -m scripts.run_batch --day 2022-09-14 --top-n 50 --spend-cap 20 --out runs/rep1_2022-09-14.json
    (repeat with rep2, rep3)
Then:
    .venv/bin/python -m scripts.compare_repeats runs/rep1_2022-09-14.json runs/rep2_2022-09-14.json runs/rep3_2022-09-14.json
"""

import argparse
import json
import sys

from src.evaluation.repeatability import compare
from src.evaluation.runs import load_run


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "runs",
        nargs="+",
        help="Two or more run_batch --out files of the same day and top N.",
    )
    parser.add_argument("--out", help="Also write the result here.")
    args = parser.parse_args()
    report = compare([load_run(p) for p in args.runs])
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
