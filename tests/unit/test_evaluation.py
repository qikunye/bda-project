import ast
import json
from pathlib import Path

import pandas as pd
import pytest

from src.evaluation import labels, manual_review, metrics, patterns, stats
from src.evaluation.runs import CaseOutcome, load_run


def case(cid, members, codes=(), **kw):
    return CaseOutcome(cid, tuple(members), frozenset(codes), **kw)


def test_wilson_interval_widens_with_few_cases():
    small, large = stats.wilson(1, 4), stats.wilson(100, 400)
    assert small[1] - small[0] > large[1] - large[0]
    assert stats.wilson(0, 0) is None
    assert stats.rate(0, 5)["ci95"][0] == 0.0


def test_code_rates_split_cases_with_and_without_a_code():
    cases = [case("a", ["t1"], ["FAN_OUT"]), case("b", ["t2"], ["FAN_OUT"]), case("c", ["t3"], [])]
    rates = metrics.code_rates(cases, {"t1": True, "t2": False, "t3": False})
    assert rates["FAN_OUT"]["with"]["n"] == 2 and rates["FAN_OUT"]["with"]["positives"] == 1
    assert rates["FAN_OUT"]["without"]["n"] == 1
    assert rates["_base"]["positives"] == 1


def test_a_case_is_positive_if_any_member_is():
    assert metrics.case_is_positive(case("a", ["t1", "t2"]), {"t2": True})
    assert not metrics.case_is_positive(case("a", ["t1"]), {})


def test_banded_rates_compare_inside_each_band():
    cases = [case("a", ["t1"], ["REPEAT_ACCOUNT_ALERTS"]), case("b", ["t2"], []), case("c", ["t3"], [])]
    out = metrics.banded_code_rates(cases, {"t1": True}, {"a": "top", "b": "top", "c": "bottom"})
    assert out["REPEAT_ACCOUNT_ALERTS"]["top"]["with"]["positives"] == 1
    assert out["REPEAT_ACCOUNT_ALERTS"]["bottom"]["with"]["n"] == 0


def test_pattern_match_scores_only_types_with_an_expected_finding():
    cases = [case("a", ["t1"], ["FAN_OUT"]), case("b", ["t2"], []), case("c", ["t3"], ["FAN_IN"])]
    out = metrics.pattern_match(cases, {"a": "FAN-OUT", "b": "FAN-OUT", "c": "STACK"})
    assert out["FAN-OUT"]["hit_rate"]["positives"] == 1 and out["FAN-OUT"]["cases"] == 2
    assert out["STACK"]["hit_rate"] is None and out["STACK"]["findings_fired"] == {"FAN_IN": 1}


def test_compare_runs_lists_what_s2_drops_and_adds():
    s1 = [case("a", ["t"], ["FAN_OUT", "FAN_IN"]), case("b", ["u"], [])]
    s2 = [case("a", ["t"], ["FAN_OUT", "MONEY_RETURNS"]), case("z", ["v"], ["FAN_IN"])]
    out = metrics.compare_runs(s1, s2)
    assert out["cases_in_both"] == 1
    assert out["dropped_by_s2"] == {"FAN_IN": 1} and out["added_by_s2"] == {"MONEY_RETURNS": 1}


def test_reliability_counts_outcomes_and_observations():
    s2 = [
        case("a", ["t"], validation="valid", observations=({"text": "x"},), observations_dropped=1),
        case("b", ["u"], validation="fallback"),
    ]
    out = metrics.reliability(s2)
    assert out["fallback"]["positives"] == 1 and out["observations_dropped"] == 1
    assert out["briefs_with_observations"]["positives"] == 1


def test_review_sheet_is_reproducible_and_shows_no_label():
    obs = ({"text": "It appears busy.", "evidence_ids": ["txn_a"]},)
    cases = [case(f"c{i}", [f"t{i}"], ["FAN_OUT"], observations=obs) for i in range(10)]
    first, second = manual_review.review_rows(cases, 4, seed=1), manual_review.review_rows(cases, 4, seed=1)
    assert first == second and len(first) == 4
    assert all("label" not in k and "laundering" not in k for k in first[0])


def test_tally_counts_marks_and_rejects_unknown_ones():
    rows = [{"mark": "useful"}, {"mark": "Wrong"}, {"mark": ""}]
    out = manual_review.tally(rows)
    assert out["marked"] == 2 and out["useful"]["count"] == 1 and out["wrong"]["count"] == 1
    with pytest.raises(ValueError):
        manual_review.tally([{"mark": "maybe"}])


PATTERN_TEXT = """BEGIN LAUNDERING ATTEMPT - FAN-OUT:  Max 2-degree Fan-Out
2022/09/14 10:05,011,AAA,0012,BBB,10.0,US Dollar,10.0,US Dollar,Wire,1
2022/09/14 10:20,011,AAA,0013,CCC,10.0,US Dollar,10.0,US Dollar,Wire,1
END LAUNDERING ATTEMPT - FAN-OUT
"""


def test_patterns_parse_and_match_by_sender_receiver_and_minute():
    attempts = patterns.parse_attempts(PATTERN_TEXT)
    assert attempts[0]["type"] == "FAN-OUT" and len(attempts[0]["payments"]) == 2
    scored = [
        {"transaction_id": "txn_1", "source_account_key": "11|AAA", "target_account_key": "12|BBB", "transaction_timestamp": "2022-09-14 10:05:00"},
        {"transaction_id": "txn_2", "source_account_key": "11|AAA", "target_account_key": "13|CCC", "transaction_timestamp": "2022-09-14T10:20:00"},
        {"transaction_id": "txn_3", "source_account_key": "11|AAA", "target_account_key": "13|CCC", "transaction_timestamp": "2022-09-14 10:20:30"},
    ]
    mapping, coverage = patterns.transaction_types(attempts, scored)
    assert mapping == {"txn_1": "FAN-OUT"} and coverage["ambiguous"] == 1  # txn_2 and txn_3 collide.
    assert patterns.case_types({"c": ("txn_1", "txn_9")}, mapping) == {"c": "FAN-OUT"}


def test_fetch_labels_queries_in_chunks_and_rejects_bad_ids():
    calls = []

    def run(sql):
        calls.append(sql)
        return pd.DataFrame([{"transaction_id": "txn_a", "evaluation_is_laundering": "1"}]), None

    found, rows = labels.fetch_labels(run, ["txn_a"])
    assert found == {"txn_a": True} and "evaluation_is_laundering" in calls[0]
    with pytest.raises(ValueError):
        labels.fetch_labels(run, ["x'; DROP"])


def test_load_run_reads_both_s1_and_s2_shapes(tmp_path):
    s1 = {"cases": [{"case_id": "a", "member_txn_ids": ["t"], "findings": [{"code": "FAN_OUT"}], "gaps": [], "next_checks": [], "summary": "s"}]}
    s2 = {"cases": [{"case_id": "a", "member_txn_ids": ["t"], "result": {"findings": [{"code": "FAN_IN"}], "observations": [{"text": "x"}], "stats": {"validation": "repaired", "observations_dropped": 2}}}]}
    (tmp_path / "s1.json").write_text(json.dumps(s1))
    (tmp_path / "s2.json").write_text(json.dumps(s2))
    assert load_run(tmp_path / "s1.json")[0].finding_codes == {"FAN_OUT"}
    second = load_run(tmp_path / "s2.json")[0]
    assert second.finding_codes == {"FAN_IN"} and second.validation == "repaired" and second.observations_dropped == 2


def test_the_agent_package_never_imports_the_evaluation_package():
    for path in Path("src/agent").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(n.startswith("src.evaluation") for n in names), path


def test_load_run_reads_the_run_batch_shape(tmp_path):
    batch = {"results": [{"case_id": "a", "result": {"case_id": "a", "members": [{"alert": {"txn_id": "t1"}}, {"alert": {"txn_id": "t2"}}], "findings": [{"code": "FAN_OUT"}]}}]}
    (tmp_path / "b.json").write_text(json.dumps(batch))
    loaded = load_run(tmp_path / "b.json")[0]
    assert loaded.member_txn_ids == ("t1", "t2") and loaded.finding_codes == {"FAN_OUT"}
