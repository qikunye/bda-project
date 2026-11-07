"""The fixed rules for each finding. They serve the code-only brief, the validator and the displayed sentences."""

import pandas as pd
import pytest

from src import config
from src.agent import findings, tools
from tests.unit.agent_loop_fixtures import (
    ACTIVITY_COLUMNS,
    activity_result,
    alert_result,
    graph_result,
    make_evidence,
    neighbour_scores_result,
    related_result,
)


def codes(evidence):
    return [i.code for i in findings.detect(evidence)]


def instance(evidence, code):
    return next(i for i in findings.detect(evidence) if i.code == code)


# --- PASS_THROUGH


def test_pass_through_needs_money_in_and_a_fast_share_out():
    found = instance(make_evidence(), "PASS_THROUGH")
    assert found.core == {"txn_high", "txn_up"}  # The first payment out and one payment in.
    assert found.ids[:2] == ("txn_high", "txn_up")
    assert "80% of the alert amount" in found.sentence
    assert "15 minutes after the alert" in found.sentence


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"down_gap": config.AGENT_PASS_THROUGH_MAX_GAP_MIN + 1}, "too slow"),
        ({"down_amount": 100.0}, "too small a share"),
        ({"upstream": ()}, "nothing came in"),
        ({"downstream": ()}, "nothing went on"),
    ],
)
def test_pass_through_is_not_found_when_a_condition_fails(kwargs, reason):
    evidence = make_evidence(ranks={})
    evidence.record("get_case_graph", graph_result("txn_a", **kwargs))
    assert "PASS_THROUGH" not in codes(evidence), reason


def test_pass_through_at_the_exact_thresholds_is_found():
    evidence = make_evidence(ranks={})
    evidence.record(
        "get_case_graph",
        graph_result(
            "txn_a",
            down_gap=config.AGENT_PASS_THROUGH_MAX_GAP_MIN,
            down_amount=1000.5 * config.AGENT_PASS_THROUGH_MIN_ONWARD_SHARE / 2,
        ),
    )
    assert "PASS_THROUGH" in codes(evidence)


def test_pass_through_is_judged_against_the_alert_amount_not_what_the_sender_took_in():
    # The sender took in 1,000,000 and the alert sent 1,000.5 on. The receiver passed all of it on.
    evidence = make_evidence(ranks={})
    evidence.record(
        "get_case_graph",
        graph_result("txn_a", up_amount=1_000_000.0, down_amount=500.25),  # Two payments of 500.25.
    )
    assert "PASS_THROUGH" in codes(evidence)
    assert "100% of the alert amount" in instance(evidence, "PASS_THROUGH").sentence


def test_a_large_inflow_to_the_sender_does_not_make_a_small_onward_payment_pass_through():
    evidence = make_evidence(ranks={})
    evidence.record("get_case_graph", graph_result("txn_a", up_amount=10.0, down_amount=100.0))
    assert "PASS_THROUGH" not in codes(evidence)  # 200 out of 1,000.5 is only 20% of the alert.


def test_onward_payments_in_another_currency_cannot_be_compared_with_the_alert():
    # The alert is in dollars. Euros came in and went out, but that says nothing about the alert.
    alert = alert_result("txn_a")
    frame = pd.DataFrame(
        [
            {
                "edge_role": role,
                "transaction_id": txn,
                "source_account_key": "9|SRC" if role == "UPSTREAM" else alert["receiver"],
                "target_account_key": alert["sender"] if role == "UPSTREAM" else "3|C",
                "transaction_timestamp": "2022-09-14 10:10:00.000",
                "gap_minutes": "10",
                "amount_paid": "5000.000000",
                "payment_currency": "Euro",
                "payment_format": "ACH",
            }
            for role, txn in (("UPSTREAM", "txn_eu_up"), ("DOWNSTREAM", "txn_eu_down"))
        ]
    )
    evidence = make_evidence(ranks={})
    evidence.record("get_case_graph", tools.case_graph_from_frame(alert, frame, 48))
    assert evidence.graphs["txn_a"]["flow"]["Euro"]["onward_share"] is None
    assert "PASS_THROUGH" not in codes(evidence)


def test_pass_through_never_mixes_currencies():
    # Money came in as dollars and went out as euros: no currency has both sides.
    alert = alert_result("txn_a")
    frame = pd.DataFrame(
        [
            {
                "edge_role": "UPSTREAM",
                "transaction_id": "txn_up",
                "source_account_key": "9|SRC",
                "target_account_key": alert["sender"],
                "transaction_timestamp": "2022-09-14 09:00:00.000",
                "gap_minutes": "60",
                "amount_paid": "1000.000000",
                "payment_currency": "US Dollar",
                "payment_format": "ACH",
            },
            {
                "edge_role": "DOWNSTREAM",
                "transaction_id": "txn_down",
                "source_account_key": alert["receiver"],
                "target_account_key": "3|C",
                "transaction_timestamp": "2022-09-14 10:10:00.000",
                "gap_minutes": "10",
                "amount_paid": "1000.000000",
                "payment_currency": "Euro",
                "payment_format": "ACH",
            },
        ]
    )
    evidence = make_evidence(ranks={})
    evidence.record("get_case_graph", tools.case_graph_from_frame(alert, frame, 48))
    assert "PASS_THROUGH" not in codes(evidence)


def test_thresholds_come_from_the_config(monkeypatch):
    evidence = make_evidence(ranks={})
    assert "PASS_THROUGH" in codes(evidence)
    monkeypatch.setattr(config, "AGENT_PASS_THROUGH_MAX_GAP_MIN", 10)
    assert "PASS_THROUGH" not in codes(evidence)


# --- FAN_OUT and FAN_IN


def test_fan_out_counts_distinct_counterparties_of_the_sender():
    enough = config.AGENT_FAN_MIN_COUNTERPARTIES
    assert "FAN_OUT" in codes(make_evidence(fan=enough))
    assert "FAN_OUT" not in codes(make_evidence(fan=enough - 1))
    found = instance(make_evidence(fan=enough), "FAN_OUT")
    assert f"{enough} different accounts" in found.sentence
    assert found.ids[0] == "txn_so1" and found.core == frozenset()


def test_many_payments_to_the_same_few_accounts_is_not_a_fan_out():
    alert = alert_result("txn_a")
    rows = [
        {
            "direction": "out",
            "transaction_id": f"txn_many{n}",
            "counterparty_key": f"8|O{n % 2}",
            "transaction_timestamp": "2022-09-14 10:30:00.000",
            "offset_minutes": str(n),
            "amount_paid": "10.000000",
            "payment_currency": "US Dollar",
            "payment_format": "ACH",
        }
        for n in range(1, 9)
    ]
    evidence = make_evidence(ranks={})
    evidence.record(
        "get_account_activity",
        tools.account_activity_from_frames(
            alert, "sender", pd.DataFrame(rows, columns=ACTIVITY_COLUMNS), None, 48
        ),
    )
    assert "FAN_OUT" not in codes(evidence)


def test_fan_in_uses_the_receivers_incoming_payments():
    enough = config.AGENT_FAN_MIN_COUNTERPARTIES
    evidence = make_evidence(incoming=enough)
    found = instance(evidence, "FAN_IN")
    assert "was paid by" in found.sentence
    assert found.ids[0] == "txn_ri1"
    # Many payments in to the sender are not a fan-in on the receiver.
    other = make_evidence()
    other.record("get_account_activity", activity_result("txn_a", "sender", incoming=enough))
    assert "FAN_IN" not in codes(other)


# --- MONEY_RETURNS


def test_money_returns_cites_both_payments():
    evidence = make_evidence(ranks={})
    evidence.record("get_case_graph", graph_result("txn_a", returned_to="1|A"))
    found = [i for i in findings.detect(evidence) if i.code == "MONEY_RETURNS"]
    assert len(found) == 2  # Two downstream payments went back to the sender.
    assert found[0].core == {"txn_high", "txn_a"}
    assert "the alert's sender" in found[0].sentence


def test_money_returns_to_an_account_that_paid_in():
    evidence = make_evidence(ranks={})
    evidence.record("get_case_graph", graph_result("txn_a", returned_to="9|SRC"))
    found = instance(evidence, "MONEY_RETURNS")
    assert found.core == {"txn_high", "txn_up"}
    assert "had paid the alert's sender" in found.sentence


def test_no_money_returns_when_it_goes_elsewhere():
    assert "MONEY_RETURNS" not in codes(make_evidence())


# --- NEIGHBOURS_HIGH_RISK and REPEAT_ACCOUNT_ALERTS


def test_neighbours_in_the_top_k_are_cited_best_rank_first():
    evidence = make_evidence(ranks={"txn_low": 400, "txn_high": 12, "txn_up": 900})
    found = instance(evidence, "NEIGHBOURS_HIGH_RISK")
    assert found.ids == ("txn_high", "txn_low")
    assert found.sentence.endswith("2 of 3.")


def test_a_scored_related_alert_is_not_a_neighbour():
    # Related alerts are top-K by definition, so scoring them would always fire this finding.
    evidence = make_evidence(ranks={"txn_low": 900}, related=related_result("txn_x"))
    evidence.record("get_neighbour_scores", neighbour_scores_result({"txn_x": 3}))
    assert "NEIGHBOURS_HIGH_RISK" not in codes(evidence)
    assert "REPEAT_ACCOUNT_ALERTS" in codes(evidence)


def test_a_scored_payment_from_account_activity_is_a_neighbour():
    evidence = make_evidence(ranks={"txn_low": 900}, fan=1)
    evidence.record("get_neighbour_scores", neighbour_scores_result({"txn_so1": 40}))
    assert instance(evidence, "NEIGHBOURS_HIGH_RISK").ids == ("txn_so1",)


def test_a_scored_payment_no_graph_or_activity_listed_is_not_a_neighbour():
    evidence = make_evidence(ranks={"txn_low": 900, "txn_far": 7})
    assert "NEIGHBOURS_HIGH_RISK" not in codes(evidence)


def test_neighbours_outside_the_top_k_or_unscored_are_not_found():
    evidence = make_evidence(ranks={"txn_low": 900, "txn_high": None})
    assert "NEIGHBOURS_HIGH_RISK" not in codes(evidence)


def test_a_case_member_is_not_a_neighbour():
    evidence = make_evidence(members=("txn_a", "txn_b"), ranks={"txn_b": 2, "txn_low": 900})
    assert "NEIGHBOURS_HIGH_RISK" not in codes(evidence)


def test_repeat_account_alerts_cites_the_related_alerts():
    evidence = make_evidence(related=related_result("txn_x", "txn_y"))
    found = instance(evidence, "REPEAT_ACCOUNT_ALERTS")
    assert found.ids == ("txn_x", "txn_y")
    assert "top 500" in found.sentence and ": 2," in found.sentence


def test_other_case_members_are_not_repeat_alerts():
    # Members share accounts by construction, so only alerts outside the case count.
    evidence = make_evidence(members=("txn_a", "txn_b"), related=related_result("txn_b"))
    assert "REPEAT_ACCOUNT_ALERTS" not in codes(evidence)
    mixed = make_evidence(members=("txn_a", "txn_b"), related=related_result("txn_b", "txn_x"))
    assert instance(mixed, "REPEAT_ACCOUNT_ALERTS").ids == ("txn_x",)


def test_no_related_alerts_or_no_call_is_not_a_finding():
    assert "REPEAT_ACCOUNT_ALERTS" not in codes(make_evidence(related=related_result()))
    assert "REPEAT_ACCOUNT_ALERTS" not in codes(make_evidence())


# --- No activity-spike finding


def test_activity_spike_is_not_a_finding():
    # Dropped before the freeze: the history (from 11 Sep) overlaps the window and differs in length
    # by day, so a jump in activity could not be judged fairly.
    assert "ACTIVITY_SPIKE" not in findings.FINDING_CODES


# --- Checking a finding


def test_check_finding_accepts_a_supported_finding():
    instances = findings.detect(make_evidence())
    assert findings.check_finding("PASS_THROUGH", {"txn_high", "txn_up"}, instances) is None
    assert findings.check_finding("PASS_THROUGH", {"txn_high", "txn_up", "txn_low"}, instances) is None


def test_check_finding_names_what_a_missing_finding_needs():
    instances = findings.detect(make_evidence())
    message = findings.check_finding("FAN_OUT", {"txn_so1"}, instances)
    assert "not supported by the tool results" in message
    assert "side=sender" in message


def test_check_finding_rejects_ids_outside_what_the_rule_found():
    instances = findings.detect(make_evidence())
    message = findings.check_finding("PASS_THROUGH", {"txn_high", "txn_up", "txn_zzz"}, instances)
    assert "txn_zzz" in message


def test_check_finding_requires_the_core_payments():
    instances = findings.detect(make_evidence())
    assert "missing required evidence" in findings.check_finding(
        "PASS_THROUGH", {"txn_low"}, instances
    )
    assert "missing required evidence" in findings.check_finding(
        "PASS_THROUGH", {"txn_high"}, instances
    )


def test_every_finding_code_has_a_rule_and_a_requirement():
    assert set(findings.REQUIREMENTS) == set(findings.FINDING_CODES)
    assert tuple(findings._DETECTORS) == findings.FINDING_CODES


def test_detect_lists_instances_in_finding_code_order():
    evidence = make_evidence(fan=6, related=related_result("txn_x"))
    order = [findings.FINDING_CODES.index(i.code) for i in findings.detect(evidence)]
    assert order == sorted(order)


# --- What the code-only brief cites


def test_s1_cites_the_core_first_and_caps_the_ids(monkeypatch):
    monkeypatch.setattr(config, "AGENT_S1_MAX_EVIDENCE", 2)
    chosen = dict(findings.s1_findings(findings.detect(make_evidence(fan=8))))
    assert chosen["PASS_THROUGH"] == ["txn_high", "txn_up"]
    assert len(chosen["FAN_OUT"]) == 2


def test_s1_cites_nothing_for_a_rule_that_did_not_fire():
    chosen = dict(findings.s1_findings(findings.detect(make_evidence(ranks={}))))
    assert set(chosen) == {"PASS_THROUGH"}
