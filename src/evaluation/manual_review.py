"""Builds the sheet for reading observations by hand, and tallies the marks.
No label is shown on the sheet, so the marks cannot be swayed by it."""

import random

from src.evaluation.runs import CaseOutcome

MARKS = ("useful", "obvious", "wrong")


def review_rows(cases: list[CaseOutcome], n_briefs: int = 50, seed: int = 0) -> list[dict]:
    """A reproducible random sample of briefs, one row per observation."""

    pool = [c for c in cases if c.observations]
    chosen = random.Random(seed).sample(pool, min(n_briefs, len(pool)))
    rows = []
    for case in sorted(chosen, key=lambda c: c.case_id):
        for index, obs in enumerate(case.observations):
            rows.append(
                {
                    "case_id": case.case_id,
                    "observation": index,
                    "text": obs["text"],
                    "evidence_ids": ";".join(obs.get("evidence_ids", [])),
                    "findings_in_brief": ";".join(sorted(case.finding_codes)),
                    "mark": "",  # useful / obvious / wrong
                }
            )
    return rows


def tally(rows: list[dict]) -> dict:
    marked = [r["mark"].strip().lower() for r in rows if r["mark"].strip()]
    unknown = sorted({m for m in marked if m not in MARKS})
    if unknown:
        raise ValueError(f"Unknown marks {unknown}; use {MARKS}.")
    n = len(marked)
    return {
        "rows": len(rows),
        "marked": n,
        **{m: {"count": marked.count(m), "share": round(marked.count(m) / n, 3) if n else None} for m in MARKS},
    }
