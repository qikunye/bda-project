"""Summarises and prints how an LLM run behaved: outcome rates, tokens, cost and latency."""

import statistics
from collections import Counter


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


def fallback_reason(result: dict) -> str | None:
    for step in result["trace"]:
        if step["tool"] == "fallback":
            return step["result"]["reason"]
    return None


def rejection_reasons(result: dict) -> list[str]:
    """Lists the validation errors that rejected the model's briefs in this run."""

    return [
        error
        for step in result["trace"]
        if step["tool"] == "submit_brief" and not step["result"]["valid"]
        for error in step["result"]["errors"]
    ]


def summarise_runs(results: list[dict], failures: list[dict], spent_usd: float) -> dict:
    """Counts outcomes and tokens across runs. ``failures`` are runs that crashed."""

    runs = [r for r in results if r["status"] == "ok"]
    n = len(runs)
    outcomes = Counter(r["stats"]["validation"] for r in runs)

    def rate(count: int) -> float | None:
        return round(count / n, 3) if n else None

    return {
        "cases_run": len(results) + len(failures),
        "cases_failed_with_error": len(failures),
        "not_found": sum(r["status"] == "not_found" for r in results),
        "valid": outcomes["valid"],
        "repaired": outcomes["repaired"],
        "fallback": outcomes["fallback"],
        "valid_rate": rate(outcomes["valid"]),
        "repair_rate": rate(outcomes["repaired"]),
        "fallback_rate": rate(outcomes["fallback"]),
        "fallback_reasons": dict(
            Counter(fallback_reason(r) for r in runs if r["stats"]["fallback"])
        ),
        "rejected_brief_reasons": dict(
            Counter(
                reason[:90] for r in runs for reason in rejection_reasons(r)
            ).most_common(10)
        ),
        "finding_counts": dict(
            Counter(f["code"] for r in runs for f in r["findings"]).most_common()
        ),
        "cases_with_findings": sum(bool(r["findings"]) for r in runs),
        "cases_with_observations": sum(bool(r["observations"]) for r in runs),
        "observations": sum(len(r["observations"]) for r in runs),
        # .get: results saved before this field existed have none.
        "observations_dropped": sum(r["stats"].get("observations_dropped", 0) for r in runs),
        "gap_counts": dict(
            Counter(g["code"] for r in runs for g in r["gaps"]).most_common()
        ),
        "steps": _spread([r["stats"]["steps"] for r in runs]),
        "tokens_in_per_run": _spread([r["stats"]["tokens_in"] for r in runs]),
        "tokens_out_per_run": _spread([r["stats"]["tokens_out"] for r in runs]),
        "latency_ms_per_run": _spread([r["stats"]["latency_ms"] for r in runs]),
        "estimated_llm_spend_usd": round(spent_usd, 4),
        "estimated_usd_per_run": round(spent_usd / n, 4) if n else None,
    }


def print_summary(summary: dict, athena_gb: float, seconds: float) -> None:
    print(
        f"Cases run: {summary['cases_run']} ({summary['cases_failed_with_error']} errored)"
    )
    for key, rate in (
        ("valid", "valid_rate"),
        ("repaired", "repair_rate"),
        ("fallback", "fallback_rate"),
    ):
        print(f"  {key}: {summary[key]} ({summary[rate]})")
    if summary["fallback_reasons"]:
        print("Fallback reasons:", summary["fallback_reasons"])
    if summary["rejected_brief_reasons"]:
        print("Most common rejections:")
        for reason, count in summary["rejected_brief_reasons"].items():
            print(f"  {count} x {reason}")
    print(f"Cases with at least one finding: {summary['cases_with_findings']}")
    print(
        f"Observations: {summary['observations']} in {summary['cases_with_observations']} cases, "
        f"{summary['observations_dropped']} dropped by the validator"
    )
    print("Findings:", summary["finding_counts"])
    print("Gaps:", summary["gap_counts"])
    for key in (
        "steps",
        "tokens_in_per_run",
        "tokens_out_per_run",
        "latency_ms_per_run",
    ):
        print(f"{key}: {summary[key]}")
    print(
        f"Estimated LLM spend: US${summary['estimated_llm_spend_usd']} "
        f"(US${summary['estimated_usd_per_run']} per run, from list prices)"
    )
    print(f"Athena: {athena_gb:.2f} GB scanned, {seconds / 60:.1f} min in total")
