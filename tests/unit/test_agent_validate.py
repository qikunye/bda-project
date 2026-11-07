import pytest

from src import config
from src.agent.validate import validate_brief
from tests.unit.agent_loop_fixtures import (
    graph_result,
    make_evidence,
    related_result,
    valid_payload,
)


def errors_of(payload, evidence=None):
    return validate_brief(payload, evidence or make_evidence()).errors


def check(payload, evidence=None):
    validated = validate_brief(payload, evidence or make_evidence())
    assert validated.errors == ()
    assert validated.ok
    return validated.brief


def rejected(payload, evidence=None, *, mentioning=""):
    errors = errors_of(payload, evidence)
    assert errors, "The brief should have been rejected."
    assert mentioning in " ".join(errors)


# --- A valid brief


def test_a_supported_brief_is_accepted_as_submitted():
    brief = check(valid_payload())
    assert [f["code"] for f in brief["findings"]] == ["PASS_THROUGH", "NEIGHBOURS_HIGH_RISK"]
    assert brief["next_checks"] == ["CHECK_ONWARD_ACCOUNTS"]
    assert brief["summary"] == "Money came in and went on quickly."


def test_an_empty_brief_is_valid():
    brief = check(valid_payload(findings=[], next_checks=[]))
    assert brief["findings"] == [] and brief["next_checks"] == []


def test_repeated_ids_and_checks_are_dropped_and_the_summary_trimmed():
    brief = check(
        valid_payload(
            findings=[{"code": "PASS_THROUGH", "evidence_ids": ["txn_high", "txn_up", "txn_up"]}],
            next_checks=["CHECK_ONWARD_ACCOUNTS", "CHECK_ONWARD_ACCOUNTS"],
            summary="  Money moved on.  ",
        )
    )
    assert brief["findings"][0]["evidence_ids"] == ["txn_high", "txn_up"]
    assert brief["next_checks"] == ["CHECK_ONWARD_ACCOUNTS"]
    assert brief["summary"] == "Money moved on."


# --- Shape


@pytest.mark.parametrize("payload", [None, [], "brief", 3])
def test_the_brief_must_be_an_object(payload):
    rejected(payload, mentioning="JSON object")


def test_unknown_fields_are_rejected_and_code_owned_ones_are_explained():
    rejected(valid_payload(color="red"), mentioning="Unknown field")
    rejected(valid_payload(gaps=[]), mentioning="set by code")
    rejected(valid_payload(money_flow=[]), mentioning="set by code")
    rejected(valid_payload(case_id="case_txn_a"), mentioning="set by code")
    rejected(valid_payload(verdict="raise"), mentioning="Unknown field")


@pytest.mark.parametrize("missing", ["findings", "next_checks", "summary"])
def test_every_field_is_required(missing):
    payload = valid_payload()
    del payload[missing]
    rejected(payload, mentioning=missing)


def test_findings_must_be_well_formed():
    rejected(valid_payload(findings="PASS_THROUGH"), mentioning="list of")
    rejected(valid_payload(findings=["PASS_THROUGH"]), mentioning="exactly the fields")
    rejected(
        valid_payload(findings=[{"code": "PASS_THROUGH", "evidence_ids": ["txn_up"], "x": 1}]),
        mentioning="exactly the fields",
    )
    rejected(
        valid_payload(findings=[{"code": "FAST_CHAIN", "evidence_ids": ["txn_up"]}]),
        mentioning="Unknown finding code",
    )
    rejected(
        valid_payload(findings=[{"code": "PASS_THROUGH", "evidence_ids": []}]),
        mentioning="at least one evidence ID",
    )
    rejected(
        valid_payload(findings=[{"code": "PASS_THROUGH", "evidence_ids": "txn_up"}]),
        mentioning="list of ID strings",
    )
    rejected(
        valid_payload(findings=[{"code": "PASS_THROUGH", "evidence_ids": [1]}]),
        mentioning="list of ID strings",
    )


def test_a_finding_code_may_appear_once():
    finding = {"code": "PASS_THROUGH", "evidence_ids": ["txn_high", "txn_up"]}
    rejected(valid_payload(findings=[finding, finding]), mentioning="appears twice")


def test_a_finding_may_cite_only_so_many_ids():
    ids = [f"txn_{n}" for n in range(config.AGENT_MAX_EVIDENCE_IDS + 1)]
    rejected(
        valid_payload(findings=[{"code": "NEIGHBOURS_HIGH_RISK", "evidence_ids": ids}]),
        mentioning="The limit is",
    )


def test_next_checks_must_be_known_codes():
    rejected(valid_payload(next_checks="CHECK_ONWARD_ACCOUNTS"), mentioning="list of codes")
    rejected(valid_payload(next_checks=["CALL_THE_POLICE"]), mentioning="Unknown next check")


@pytest.mark.parametrize("summary", [None, "", "   ", 5])
def test_the_summary_must_be_a_sentence(summary):
    rejected(valid_payload(summary=summary), mentioning="summary")


# --- IDs and rules


def test_ids_no_tool_returned_are_rejected():
    rejected(
        valid_payload(findings=[{"code": "NEIGHBOURS_HIGH_RISK", "evidence_ids": ["txn_invented"]}]),
        mentioning="no tool returned",
    )


def test_a_finding_the_rules_do_not_find_is_rejected_with_what_it_needs():
    payload = valid_payload(
        findings=[{"code": "FAN_OUT", "evidence_ids": ["txn_high"]}],
        next_checks=[],
    )
    rejected(payload, mentioning="side=sender")


def test_a_finding_needs_its_core_evidence():
    payload = valid_payload(
        findings=[{"code": "PASS_THROUGH", "evidence_ids": ["txn_low"]}], next_checks=[]
    )
    rejected(payload, mentioning="missing required evidence")


def test_a_finding_cannot_cite_ids_outside_what_the_rule_found():
    payload = valid_payload(
        findings=[{"code": "NEIGHBOURS_HIGH_RISK", "evidence_ids": ["txn_low"]}],  # Rank 900.
        next_checks=[],
    )
    rejected(payload, mentioning="txn_low")


def test_a_finding_is_judged_on_this_runs_tool_results_only():
    evidence = make_evidence(ranks={})  # The model never fetched neighbour scores.
    rejected(
        valid_payload(
            findings=[{"code": "NEIGHBOURS_HIGH_RISK", "evidence_ids": ["txn_high"]}],
            next_checks=[],
        ),
        evidence,
        mentioning="not supported by the tool results",
    )


def test_money_returns_needs_both_payments():
    evidence = make_evidence(ranks={})
    evidence.record("get_case_graph", graph_result("txn_a", returned_to="9|SRC"))
    both = [{"code": "MONEY_RETURNS", "evidence_ids": ["txn_high", "txn_up"]}]
    one = [{"code": "MONEY_RETURNS", "evidence_ids": ["txn_high"]}]
    check(valid_payload(findings=both, next_checks=["CHECK_RETURN_FLOW"]), evidence)
    rejected(valid_payload(findings=one, next_checks=[]), evidence, mentioning="missing required")


def test_fan_out_needs_enough_counterparties():
    finding = [{"code": "FAN_OUT", "evidence_ids": ["txn_so1", "txn_so2"]}]
    check(valid_payload(findings=finding), make_evidence(fan=config.AGENT_FAN_MIN_COUNTERPARTIES))
    rejected(
        valid_payload(findings=finding),
        make_evidence(fan=config.AGENT_FAN_MIN_COUNTERPARTIES - 1),
        mentioning="not supported",
    )


def test_repeat_alerts_cite_what_get_related_alerts_returned():
    evidence = make_evidence(related=related_result("txn_x"))
    ok = [{"code": "REPEAT_ACCOUNT_ALERTS", "evidence_ids": ["txn_x"]}]
    check(valid_payload(findings=ok, next_checks=["REVIEW_RELATED_ALERTS"]), evidence)
    bad = [{"code": "REPEAT_ACCOUNT_ALERTS", "evidence_ids": ["txn_low"]}]
    rejected(valid_payload(findings=bad, next_checks=[]), evidence, mentioning="txn_low")


# --- Next checks


def test_a_next_check_needs_a_finding_or_gap_that_allows_it():
    rejected(valid_payload(next_checks=["CHECK_RETURN_FLOW"]), mentioning="MONEY_RETURNS")
    rejected(valid_payload(next_checks=["REVIEW_RELATED_ALERTS", "CHECK_ACCOUNT_HISTORY"]), mentioning="CHECK_ACCOUNT_HISTORY")


def test_a_next_check_may_follow_from_a_gap_code_sets():
    evidence = make_evidence(truncated=True)
    check(
        valid_payload(findings=[], next_checks=["RECHECK_AFTER_MORE_DATA"]), evidence
    )
    # No gap, so the same check is refused.
    rejected(
        valid_payload(findings=[], next_checks=["RECHECK_AFTER_MORE_DATA"]),
        mentioning="RECHECK_AFTER_MORE_DATA",
    )


def test_not_checking_account_history_allows_checking_it():
    evidence = make_evidence(activities=False)
    check(valid_payload(findings=[], next_checks=["CHECK_ACCOUNT_HISTORY"]), evidence)


# --- Summary


@pytest.mark.parametrize(
    "summary",
    [
        "It moved 3 times.",
        "Over 5 accounts.",
        "Roughly fifty accounts.",
        "Two accounts were involved.",
        "Hundreds of payments.",
        "About 1,000 dollars.",
    ],
)
def test_the_summary_carries_no_numbers(summary):
    rejected(valid_payload(summary=summary), mentioning="summary")


def test_the_word_one_is_allowed_in_the_summary():
    check(valid_payload(summary="One of the accounts paid many others."))


def test_the_summary_is_limited_in_length():
    check(valid_payload(summary="x" * config.AGENT_SUMMARY_MAX_CHARS))
    rejected(valid_payload(summary="x" * (config.AGENT_SUMMARY_MAX_CHARS + 1)), mentioning="limit")


def test_all_problems_are_reported_together():
    errors = errors_of(
        valid_payload(
            findings=[{"code": "FAN_OUT", "evidence_ids": ["txn_invented"]}],
            next_checks=["CHECK_RETURN_FLOW"],
            summary="It moved 3 times.",
        )
    )
    assert len(errors) >= 4
