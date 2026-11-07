"""Puts the per-day evaluation reports into one Markdown summary, with example briefs.

Example briefs are picked without labels (by validation outcome and number of findings), so the
choice cannot be swayed by which alerts turn out to be laundering.

    .venv/bin/python -m scripts.summarise_evaluation --eval runs/eval_2022-09-1{4,5,6}.json \\
        --s2 runs/s2_2022-09-1{4,5,6}.json --out runs/evaluation_summary.md
"""

import argparse
import json
import sys
from pathlib import Path

from src.evaluation.runs import load_run


def _pct(r: dict | None) -> str:
    if not r or r.get("rate") is None:
        return "n/a"
    lo, hi = r["ci95"]
    return f"{r['rate']:.1%} ({r['positives']}/{r['n']}, 95% CI {lo:.1%}-{hi:.1%})"


def day_section(report: dict) -> list[str]:
    rel, cost = report.get("reliability_s2", {}), report.get("cost_latency_s2", {})
    lf = report.get("labels_found", {})
    lines = [
        f"## {report['day']}",
        "",
        f"- Alerts with a label: {lf.get('with_label')} of {lf.get('alerts')}; laundering: {lf.get('positives')}",
        f"- S2 outcomes: {rel.get('validation')}; fallback {_pct(rel.get('fallback'))}; repaired {_pct(rel.get('repaired'))}",
        f"- Observations kept {rel.get('observations_kept')}, dropped {rel.get('observations_dropped')}",
    ]
    if cost:
        lines.append(
            f"- Cost: about US${cost['estimated_spend_usd_total']} "
            f"(US${cost['estimated_spend_usd_per_case']} a case); latency median "
            f"{cost['latency_s_per_case'].get('median')} s, p90 {cost['latency_s_per_case'].get('p90')} s"
        )
    lines += [
        "",
        "| Finding | S1 with | S1 without | S2 with | S2 without |",
        "|---|---|---|---|---|",
    ]
    s1, s2 = report.get("s1_code_rates", {}), report.get("s2_code_rates", {})
    for code in [k for k in s1 if not k.startswith("_")]:
        lines.append(
            f"| {code} | {_pct(s1[code]['with'])} | {_pct(s1[code]['without'])} | "
            f"{_pct(s2.get(code, {}).get('with'))} | {_pct(s2.get(code, {}).get('without'))} |"
        )
    lines.append(f"\nBase rate (S1 cases): {_pct(s1.get('_base'))}")
    vs = report.get("s2_vs_s1", {})
    lines.append(
        f"\nS2 vs S1: dropped {vs.get('dropped_by_s2')}, added {vs.get('added_by_s2')}"
    )
    if "observation_marks" in report:
        m = report["observation_marks"]
        lines.append(
            f"\nObservations marked: {m['marked']}; useful {m['useful']['share']}, "
            f"obvious {m['obvious']['share']}, wrong {m['wrong']['share']}"
        )
    if "s1_deep" in report:
        lines.append(
            f"\nDeeper S1 list: {report['s1_deep']['cases']} cases (rank bands in the JSON report)."
        )
    lines.append(f"\nPattern coverage: {report.get('pattern_coverage')}")
    return lines


def pick_examples(s2_path: str) -> list[tuple[str, dict]]:
    """A good brief (valid, most findings), an ordinary one (valid, no finding) and a fallback."""

    raw = json.loads(Path(s2_path).read_text())
    results = {r["case_id"]: r["result"] for r in raw.get("results", [])}
    cases = load_run(s2_path)
    valid = [c for c in cases if c.validation == "valid"]
    picks = []
    if valid:
        picks.append(
            (
                "Good (valid, most findings)",
                max(valid, key=lambda c: (len(c.finding_codes), len(c.observations))),
            )
        )
        plain = [c for c in valid if not c.finding_codes]
        if plain:
            picks.append(("Ordinary (valid, no finding)", plain[0]))
    fallbacks = [c for c in cases if c.validation == "fallback"]
    if fallbacks:
        picks.append(("Fallback (code-only brief)", fallbacks[0]))
    return [(name, results.get(c.case_id, {})) for name, c in picks]


def brief_markdown(name: str, result: dict) -> list[str]:
    lines = [
        f"### {name}: {result.get('case_id') or result.get('txn_id')}",
        "",
        f"Summary: {result.get('summary')}",
        "",
    ]
    lines += [
        f"- Finding: {f.get('sentence', f.get('code'))}"
        for f in result.get("findings", [])
    ]
    lines += [
        f"- Gap: {g.get('sentence', g.get('code'))}" for g in result.get("gaps", [])
    ]
    lines += [
        f"- Next check: {n.get('sentence', n.get('code'))}"
        for n in result.get("next_checks", [])
    ]
    lines += [f"- Observation: {o.get('text')}" for o in result.get("observations", [])]
    return lines + [""]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--eval",
        nargs="+",
        required=True,
        help="run_evaluation --out files, one per day.",
    )
    parser.add_argument(
        "--s2", nargs="*", default=[], help="run_batch --out files, for example briefs."
    )
    parser.add_argument("--repeat", help="Optional compare_repeats --out file.")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    lines = [
        "# Agent evaluation summary",
        "",
        "Synthetic IBM AML data (LI-Medium); test days 14-16 Sep 2022, reported per day.",
        "",
    ]
    for path in sorted(args.eval):
        lines += day_section(json.loads(Path(path).read_text())) + [""]
    if args.repeat:
        rep = json.loads(Path(args.repeat).read_text())
        lines += [
            "## Repeatability",
            "",
            (
                f"{rep['runs']} runs, {rep['cases_compared']} cases: same findings {_pct(rep['same_findings_every_run'])}, "
                f"same next checks {_pct(rep['same_next_checks_every_run'])}, "
                f"same outcome {_pct(rep['same_validation_outcome_every_run'])}, "
                f"mean finding Jaccard {rep['mean_pairwise_finding_jaccard']}"
            ),
            "",
        ]
    if args.s2:
        lines += ["## Example briefs", ""]
        for path in sorted(args.s2):
            for name, result in pick_examples(path):
                lines += brief_markdown(name, result)
    Path(args.out).write_text("\n".join(lines))
    print("Wrote", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
