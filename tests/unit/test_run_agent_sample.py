from scripts.run_agent_sample import (
    fallback_reason,
    print_summary,
    rejection_reasons,
    summarise_runs,
)


def result(
    validation="valid",
    *,
    findings=("PASS_THROUGH",),
    observations=0,
    tokens=(1000, 200),
    reason=None,
    errors=(),
):
    trace = [{"tool": "get_alert", "result": {}}]
    if errors:
        trace.append(
            {
                "tool": "submit_brief",
                "result": {"valid": False, "errors": list(errors)},
            }
        )
    if reason:
        trace.append({"tool": "fallback", "result": {"reason": reason}})
    return {
        "status": "ok",
        "findings": [{"code": code} for code in findings],
        "gaps": [{"code": "TRUNCATED_WINDOW"}],
        "observations": [{"text": "x", "evidence_ids": ["txn_a"]}] * observations,
        "stats": {
            "validation": validation,
            "fallback": validation == "fallback",
            "steps": 3,
            "tokens_in": tokens[0],
            "tokens_out": tokens[1],
            "latency_ms": 5000,
        },
        "trace": trace,
    }


def test_rates_count_each_outcome_over_finished_runs():
    runs = [
        result(),
        result(),
        result("repaired", errors=["bad id"]),
        result(
            "fallback",
            reason="Brief still invalid after one repair.",
            errors=["bad id", "bad id"],
        ),
    ]
    summary = summarise_runs(runs, [], 1.0)
    assert (summary["valid"], summary["repaired"], summary["fallback"]) == (2, 1, 1)
    assert (
        summary["valid_rate"],
        summary["repair_rate"],
        summary["fallback_rate"],
    ) == (0.5, 0.25, 0.25)
    assert summary["fallback_reasons"] == {
        "Brief still invalid after one repair.": 1
    }
    assert summary["rejected_brief_reasons"] == {"bad id": 3}
    assert summary["estimated_usd_per_run"] == 0.25


def test_tokens_are_summarised_per_run():
    summary = summarise_runs(
        [result(tokens=(1000, 100)), result(tokens=(3000, 300))], [], 0
    )
    assert summary["tokens_in_per_run"]["mean"] == 2000.0
    assert summary["tokens_in_per_run"]["max"] == 3000.0
    assert summary["tokens_out_per_run"]["median"] == 200.0


def test_errored_and_missing_cases_are_counted_separately():
    summary = summarise_runs(
        [
            result(),
            {"status": "not_found", "stats": None, "findings": [], "gaps": [], "observations": [], "trace": []},
        ],
        [{"error": "boom"}],
        0.5,
    )
    assert summary["cases_run"] == 3
    assert summary["cases_failed_with_error"] == 1
    assert summary["not_found"] == 1
    assert summary["valid_rate"] == 1.0


def test_findings_and_gaps_are_counted():
    summary = summarise_runs(
        [result(), result(findings=("PASS_THROUGH", "FAN_OUT")), result(findings=())], [], 0
    )
    assert summary["finding_counts"] == {"PASS_THROUGH": 2, "FAN_OUT": 1}
    assert summary["cases_with_findings"] == 2
    assert summary["gap_counts"] == {"TRUNCATED_WINDOW": 3}


def test_observations_are_counted():
    summary = summarise_runs([result(observations=2), result(), result(observations=1)], [], 0)
    assert summary["observations"] == 3 and summary["cases_with_observations"] == 2


def test_no_runs_gives_no_rates():
    summary = summarise_runs([], [], 0)
    assert summary["valid_rate"] is None and summary["estimated_usd_per_run"] is None


def test_helpers_read_the_trace():
    r = result("fallback", reason="x", errors=["e1"])
    assert fallback_reason(r) == "x" and rejection_reasons(r) == ["e1"]
    assert fallback_reason(result()) is None


def test_the_summary_prints_for_every_outcome(capsys):
    runs = [
        result(),
        result("repaired", errors=["bad id"]),
        result("fallback", reason="r"),
    ]
    print_summary(summarise_runs(runs, [], 0.3), athena_gb=0.5, seconds=90)
    out = capsys.readouterr().out
    assert "valid: 1" in out and "repaired: 1" in out and "fallback: 1" in out
    assert "bad id" in out and "Athena: 0.50 GB" in out


def test_the_summary_prints_with_no_runs(capsys):
    print_summary(summarise_runs([], [], 0), athena_gb=0, seconds=1)
    assert "Cases run: 0" in capsys.readouterr().out
