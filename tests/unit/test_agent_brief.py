"""What code writes into the brief: gaps, next-check rules, sentences, money flow, template summary."""

import re

import pytest

from src import config
from src.agent import brief, findings
from src.agent.contracts import CaseMember
from tests.unit.agent_loop_fixtures import (
    alert_result,
    graph_result,
    make_evidence,
)


def gap_codes(evidence):
    return [g["code"] for g in brief.compute_gaps(evidence)]


# --- Gaps


def test_a_complete_run_has_no_gaps():
    assert gap_codes(make_evidence()) == []


def test_no_payments_is_a_gap_for_the_representative():
    evidence = make_evidence(ranks={})
    evidence.record("get_case_graph", graph_result("txn_a", downstream=(), upstream=()))
    gaps = brief.compute_gaps(evidence)
    assert [g["code"] for g in gaps] == ["NO_GRAPH_AVAILABLE"]
    assert "48 h window" in gaps[0]["sentence"]


def test_no_payments_for_another_member_is_not_a_gap():
    evidence = make_evidence(members=("txn_a", "txn_b"), ranks={})
    evidence.record("get_case_graph", graph_result("txn_b", downstream=(), upstream=()))
    assert "NO_GRAPH_AVAILABLE" not in gap_codes(evidence)


def test_truncation_and_short_observation_follow_the_flags():
    evidence = make_evidence(truncated=True)  # Data visible to 12:00, two hours after the alert.
    gaps = brief.compute_gaps(evidence)
    assert [g["code"] for g in gaps] == ["TRUNCATED_WINDOW", "SHORT_OBSERVATION"]
    assert "(2022-09-14 12:00:00)" in gaps[0]["sentence"]
    assert "Only 2 h of data" in gaps[1]["sentence"]


@pytest.mark.parametrize(
    ("as_of", "short"),
    [
        ("2022-09-14 15:48:00", True),  # 5.8 hours. Hours are reported to one decimal.
        ("2022-09-14 16:00:00", False),  # Exactly six hours is enough.
    ],
)
def test_short_observation_boundary(as_of, short):
    evidence = make_evidence()
    evidence.record("get_case_graph", graph_result("txn_a", as_of=as_of))
    assert ("SHORT_OBSERVATION" in gap_codes(evidence)) is short


def test_not_checking_account_history_is_a_gap():
    gaps = brief.compute_gaps(make_evidence(activities=False))
    assert [g["code"] for g in gaps] == ["SHORT_HISTORY"]
    assert "was not checked" in gaps[0]["sentence"]


def test_too_little_history_is_a_gap(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MIN_HISTORY_DAYS", 4)  # A 14 Sep alert has 3.
    gaps = brief.compute_gaps(make_evidence())
    assert [g["code"] for g in gaps] == ["SHORT_HISTORY"]
    assert "Only 3 earlier days" in gaps[0]["sentence"]


def test_the_observation_minimum_follows_the_pass_through_gap():
    assert config.AGENT_MIN_HOURS_OBSERVED * 60 == config.AGENT_PASS_THROUGH_MAX_GAP_MIN


# --- Next checks


@pytest.mark.parametrize("check", brief.NEXT_CHECK_CODES)
def test_a_next_check_is_allowed_only_with_the_codes_it_follows_from(check):
    every = set(findings.FINDING_CODES) | set(brief.GAP_CODES)
    needs = brief.NEXT_CHECK_NEEDS[check]
    for code in needs:
        finding = {code} & set(findings.FINDING_CODES)
        gap = {code} & set(brief.GAP_CODES)
        assert check in brief.allowed_next_checks(finding, gap)
    for code in every - needs:
        finding = {code} & set(findings.FINDING_CODES)
        gap = {code} & set(brief.GAP_CODES)
        assert check not in brief.allowed_next_checks(finding, gap), (check, code)


def test_the_next_check_table_matches_the_plan():
    assert brief.NEXT_CHECK_NEEDS == {
        "CHECK_ONWARD_ACCOUNTS": {"PASS_THROUGH", "FAN_OUT"},
        "CHECK_SOURCE_ACCOUNTS": {"PASS_THROUGH", "FAN_IN"},
        "CHECK_RETURN_FLOW": {"MONEY_RETURNS"},
        "REVIEW_RELATED_ALERTS": {"REPEAT_ACCOUNT_ALERTS", "NEIGHBOURS_HIGH_RISK"},
        "CHECK_ACCOUNT_HISTORY": {"ACTIVITY_SPIKE", "SHORT_HISTORY"},
        "RECHECK_AFTER_MORE_DATA": {"SHORT_OBSERVATION", "TRUNCATED_WINDOW"},
    }


def test_allowed_checks_come_out_in_a_fixed_order():
    allowed = brief.allowed_next_checks({"FAN_IN", "PASS_THROUGH"}, {"TRUNCATED_WINDOW"})
    assert allowed == [
        "CHECK_ONWARD_ACCOUNTS",
        "CHECK_SOURCE_ACCOUNTS",
        "RECHECK_AFTER_MORE_DATA",
    ]


def test_every_next_check_has_a_sentence_in_fixed_order():
    rendered = brief.render_next_checks(["RECHECK_AFTER_MORE_DATA", "CHECK_RETURN_FLOW"])
    assert [r["code"] for r in rendered] == ["CHECK_RETURN_FLOW", "RECHECK_AFTER_MORE_DATA"]
    assert all(r["sentence"].endswith(".") for r in rendered)
    assert set(brief.NEXT_CHECK_SENTENCES) == set(brief.NEXT_CHECK_CODES)


# --- Finding sentences


def test_a_finding_gets_the_sentence_of_the_rule_that_found_it():
    instances = findings.detect(make_evidence())
    [rendered] = brief.render_findings([("PASS_THROUGH", ["txn_high", "txn_up"])], instances)
    assert rendered["code"] == "PASS_THROUGH"
    assert rendered["evidence_ids"] == ["txn_high", "txn_up"]
    assert "15 minutes after the alert" in rendered["sentence"]


def test_a_finding_seen_in_several_places_says_how_many():
    evidence = make_evidence(members=("txn_a", "txn_b"))
    evidence.record("get_alert", alert_result("txn_b"))
    evidence.record("get_case_graph", graph_result("txn_b"))
    instances = findings.detect(evidence)
    cited = ["txn_high", "txn_up"]  # Both members' graphs list these payments.
    [rendered] = brief.render_findings([("PASS_THROUGH", cited)], instances)
    assert rendered["sentence"].endswith("Seen 2 times in this case.")


def test_rendering_a_finding_the_tools_do_not_support_is_an_error():
    with pytest.raises(ValueError, match="no supporting tool result"):
        brief.render_findings([("FAN_OUT", ["txn_so1"])], findings.detect(make_evidence()))


# --- Money flow and the template summary


def test_money_flow_comes_from_the_graph():
    graph = graph_result("txn_a")
    flow = brief.build_money_flow([CaseMember(alert=alert_result("txn_a"), graph=graph)])
    assert flow == [
        {
            "txn_id": "txn_a",
            "no_graph": False,
            "n_upstream": 1,
            "n_downstream": 2,
            "per_currency": graph["flow"],
            "hours_observed_after_alert": 48.0,
        }
    ]
    assert flow[0]["per_currency"]["US Dollar"]["onward_share"] == 0.8


def test_template_summary_names_each_finding_and_has_no_numbers():
    assert brief.template_summary([]) == "The fixed rules found no pattern around this alert."
    summary = brief.template_summary(list(findings.FINDING_CODES))
    assert summary.startswith("The fixed rules found: ") and summary.endswith(".")
    assert not re.search(r"\d", summary)
    assert summary.count(",") == len(findings.FINDING_CODES) - 1


def test_the_case_id_is_built_from_the_representative():
    assert brief.case_id_for("txn_a") == "case_txn_a"
