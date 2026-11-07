"""Repeatability of the LLM brief: the same alerts run several times, compared case by case.
Label-free, so it can be run and read before the freeze."""

from collections import Counter
from itertools import combinations

from src.evaluation.runs import CaseOutcome
from src.evaluation.stats import rate

FIELDS = ("finding_codes", "next_check_codes", "validation")


def _jaccard(a: frozenset, b: frozenset) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def compare(runs: list[list[CaseOutcome]]) -> dict:
    """``runs`` holds two or more runs of the same day and top N.

    For each field, the share of cases where every run gave the same value, plus the mean pairwise
    Jaccard of finding codes and how often each finding code appears in some runs but not all.
    Cases are matched by case_id; a case missing from any run is counted, not compared.
    """

    if len(runs) < 2:
        raise ValueError("Need at least two runs to compare.")
    by_run = [{c.case_id: c for c in run} for run in runs]
    shared = sorted(set.intersection(*(set(r) for r in by_run)))
    missing = sorted(set.union(*(set(r) for r in by_run)) - set(shared))
    n = len(shared)

    identical = Counter()
    unstable_codes = Counter()
    jaccards = []
    summaries_identical = 0
    observation_counts = []
    for case_id in shared:
        versions = [r[case_id] for r in by_run]
        for name in FIELDS:
            if len({getattr(v, name) for v in versions}) == 1:
                identical[name] += 1
        if len({v.summary for v in versions}) == 1:
            summaries_identical += 1
        all_codes = frozenset().union(*(v.finding_codes for v in versions))
        always = frozenset.intersection(*(v.finding_codes for v in versions))
        unstable_codes.update(all_codes - always)
        jaccards.extend(
            _jaccard(a.finding_codes, b.finding_codes)
            for a, b in combinations(versions, 2)
        )
        observation_counts.append(tuple(len(v.observations) for v in versions))

    return {
        "runs": len(runs),
        "cases_compared": n,
        "cases_missing_from_some_run": missing,
        "same_findings_every_run": rate(identical["finding_codes"], n),
        "same_next_checks_every_run": rate(identical["next_check_codes"], n),
        "same_validation_outcome_every_run": rate(identical["validation"], n),
        "same_summary_text_every_run": rate(summaries_identical, n),
        "mean_pairwise_finding_jaccard": round(sum(jaccards) / len(jaccards), 4)
        if jaccards
        else None,
        "finding_codes_not_in_every_run": dict(unstable_codes),
        "cases_with_varying_observation_count": sum(
            len(set(c)) > 1 for c in observation_counts
        ),
    }
