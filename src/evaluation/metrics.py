"""Metrics over cases and labels. Pure functions, so they are tested with small hand-made inputs."""

from collections import Counter

from src.evaluation.runs import CaseOutcome
from src.evaluation.stats import rate

FINDING_CODES = (
    "PASS_THROUGH",
    "FAN_OUT",
    "FAN_IN",
    "MONEY_RETURNS",
    "NEIGHBOURS_HIGH_RISK",
    "REPEAT_ACCOUNT_ALERTS",
)
# These two come from BQ1 scores, so they look informative just because BQ1 is (F34).
BQ1_DERIVED = ("NEIGHBOURS_HIGH_RISK", "REPEAT_ACCOUNT_ALERTS")
# Which finding should fire for each laundering pattern type in the patterns file.
EXPECTED_FINDING = {"FAN-OUT": "FAN_OUT", "FAN-IN": "FAN_IN", "CYCLE": "MONEY_RETURNS"}


def case_is_positive(case: CaseOutcome, labels: dict[str, bool]) -> bool:
    """A case is positive when any member alert is a laundering transaction."""

    return any(labels.get(t, False) for t in case.member_txn_ids)


def code_rates(cases: list[CaseOutcome], labels: dict[str, bool]) -> dict:
    """Per finding code: the laundering rate of cases with it and without it."""

    out = {}
    for code in FINDING_CODES:
        with_ = [case_is_positive(c, labels) for c in cases if code in c.finding_codes]
        without = [case_is_positive(c, labels) for c in cases if code not in c.finding_codes]
        out[code] = {
            "with": rate(sum(with_), len(with_)),
            "without": rate(sum(without), len(without)),
        }
    out["_base"] = rate(sum(case_is_positive(c, labels) for c in cases), len(cases))
    return out


def banded_code_rates(
    cases: list[CaseOutcome],
    labels: dict[str, bool],
    band_of: dict[str, str],
    codes: tuple[str, ...] = BQ1_DERIVED,
) -> dict:
    """The same with/without comparison inside each BQ1 rank band, keyed by case_id."""

    out = {}
    for code in codes:
        out[code] = {}
        for band in sorted({band_of[c.case_id] for c in cases if c.case_id in band_of}):
            in_band = [c for c in cases if band_of.get(c.case_id) == band]
            with_ = [case_is_positive(c, labels) for c in in_band if code in c.finding_codes]
            without = [case_is_positive(c, labels) for c in in_band if code not in c.finding_codes]
            out[code][band] = {
                "with": rate(sum(with_), len(with_)),
                "without": rate(sum(without), len(without)),
            }
    return out


def pattern_match(
    cases: list[CaseOutcome], case_pattern: dict[str, str]
) -> dict:
    """For cases in a laundering attempt of a known type: did the expected finding fire?

    ``case_pattern`` maps case_id to the attempt type. Types with no matching finding code
    (stack, bipartite and so on) are only counted, never scored.
    """

    by_type: dict[str, dict] = {}
    for case in cases:
        kind = case_pattern.get(case.case_id)
        if kind is None:
            continue
        row = by_type.setdefault(kind, {"cases": 0, "expected": EXPECTED_FINDING.get(kind), "hits": 0, "fired": Counter()})
        row["cases"] += 1
        row["fired"].update(case.finding_codes)
        if row["expected"] and row["expected"] in case.finding_codes:
            row["hits"] += 1
    return {
        kind: {
            "cases": r["cases"],
            "expected_finding": r["expected"],
            "hit_rate": rate(r["hits"], r["cases"]) if r["expected"] else None,
            "findings_fired": dict(r["fired"]),
        }
        for kind, r in by_type.items()
    }


def compare_runs(s1: list[CaseOutcome], s2: list[CaseOutcome]) -> dict:
    """What S2 leaves out of S1's findings, and what it adds. Matched by case_id."""

    s1_by = {c.case_id: c for c in s1}
    dropped, added = Counter(), Counter()
    both = 0
    for case in s2:
        base = s1_by.get(case.case_id)
        if base is None:
            continue
        both += 1
        dropped.update(base.finding_codes - case.finding_codes)
        added.update(case.finding_codes - base.finding_codes)
    return {
        "cases_in_both": both,
        "s1_findings": sum(len(s1_by[c.case_id].finding_codes) for c in s2 if c.case_id in s1_by),
        "s2_findings": sum(len(c.finding_codes) for c in s2 if c.case_id in s1_by),
        "dropped_by_s2": dict(dropped),
        "added_by_s2": dict(added),
        "next_check_codes_s2": dict(Counter(code for c in s2 for code in c.next_check_codes)),
    }


def reliability(s2: list[CaseOutcome]) -> dict:
    """Label-free rates: validation outcome, dropped observations, briefs that have any."""

    n = len(s2)
    outcomes = Counter(c.validation for c in s2)
    observations = sum(len(c.observations) for c in s2)
    dropped = sum(c.observations_dropped for c in s2)
    return {
        "cases": n,
        "validation": dict(outcomes),
        "fallback": rate(outcomes["fallback"], n),
        "repaired": rate(outcomes["repaired"], n),
        "briefs_with_observations": rate(sum(bool(c.observations) for c in s2), n),
        "observations_kept": observations,
        "observations_dropped": dropped,
    }
