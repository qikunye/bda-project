"""Metrics over cases and labels. Pure functions, so they are tested with small hand-made inputs."""

import statistics
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


def same_rules_agree(s1: list[CaseOutcome], deep: list[CaseOutcome]) -> dict:
    """Label-free check that two code-only runs gave the same findings on the cases both hold.

    Only cases with the same members are compared: a deeper queue can pull lower-ranked alerts
    into a case, which changes its findings by design. S1 is deterministic, so a difference on
    an unchanged case means one run saw different data and its rates should not be reported.
    """

    deep_by = {c.case_id: c for c in deep}
    shared = [c for c in s1 if c.case_id in deep_by]
    same = [c for c in shared if c.member_txn_ids == deep_by[c.case_id].member_txn_ids]
    differ = [c.case_id for c in same if c.finding_codes != deep_by[c.case_id].finding_codes]
    return {
        "cases_in_both": len(shared),
        "same_members": len(same),
        "differ": len(differ),
        "first_differ": differ[:5],
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


def band_by_rank(cases: list[CaseOutcome], edges: tuple[int, ...] = (100, 250)) -> dict[str, str]:
    """BQ1 rank band of each case, from its representative's day rank.

    With the default edges: ``1-100``, ``101-250`` and ``251+``. Cases without a rank are left out,
    so mix rank-banded and position-banded results only with care.
    """

    labels = []
    low = 1
    for edge in edges:
        labels.append((edge, f"{low}-{edge}"))
        low = edge + 1
    tail = f"{low}+"
    out = {}
    for case in cases:
        if case.rank_day is None:
            continue
        out[case.case_id] = next((name for edge, name in labels if case.rank_day <= edge), tail)
    return out


def _spread(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "mean": round(statistics.fmean(ordered), 1),
        "median": round(statistics.median(ordered), 1),
        "p90": round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 1),
        "max": round(ordered[-1], 1),
    }


def cost_latency(runs: list[CaseOutcome], price_per_m_tokens: tuple[float, float]) -> dict:
    """Label-free: tokens, estimated LLM spend and latency per case, from each brief's stats.

    The spend is an estimate from token counts at ``price_per_m_tokens`` (input, output), the same
    way the batch's spend guard counts it; Cost Explorer reports Bedrock charges late.
    """

    tokens_in = [c.stats.get("tokens_in", 0) for c in runs]
    tokens_out = [c.stats.get("tokens_out", 0) for c in runs]
    latency_s = [c.stats.get("latency_ms", 0) / 1000 for c in runs]
    spend = [(i * price_per_m_tokens[0] + o * price_per_m_tokens[1]) / 1e6 for i, o in zip(tokens_in, tokens_out)]
    return {
        "cases": len(runs),
        "tokens_in_per_case": _spread(tokens_in),
        "tokens_out_per_case": _spread(tokens_out),
        "steps_per_case": _spread([c.stats.get("steps", 0) for c in runs]),
        "latency_s_per_case": _spread(latency_s),
        "estimated_spend_usd_total": round(sum(spend), 2),
        "estimated_spend_usd_per_case": round(sum(spend) / len(runs), 4) if runs else None,
    }
