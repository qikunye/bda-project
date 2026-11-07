import json

import pandas as pd
import pytest

from src.agent import baseline, tools
from src.agent.cases import QueueAlert, group_into_cases, queue_alerts_from_frame
from src.agent.decisions import build_decision_rows
from src.agent.evidence import RunEvidence
from src.agent.review_list import build_review_list
from src.agent.validate import validate_brief
from tests.unit.agent_loop_fixtures import (
    case_of,
    graph_result,
    stub_tools,
)

DAY = "2022-09-14"


def alert(txn_id, rank, sender="1|A", receiver="2|B", ts="2022-09-14 10:00:00"):
    return QueueAlert(txn_id, sender, receiver, ts, rank)


# --- Case grouping


def test_alerts_sharing_an_account_within_the_window_join_one_case():
    cases = group_into_cases(
        [alert("txn_b", 2, "1|A", "3|C"), alert("txn_a", 1, "1|A", "2|B")], DAY
    )
    assert len(cases) == 1
    assert cases[0].representative_txn_id == "txn_a"
    assert cases[0].member_txn_ids == ("txn_a", "txn_b")
    assert cases[0].case_id == "case_txn_a"


def test_shared_receiver_and_sender_both_link():
    cases = group_into_cases(
        [alert("txn_a", 1, "1|A", "2|B"), alert("txn_b", 2, "2|B", "3|C")], DAY
    )
    assert len(cases) == 1


def test_no_shared_account_gives_separate_cases():
    cases = group_into_cases(
        [alert("txn_a", 1, "1|A", "2|B"), alert("txn_b", 2, "3|C", "4|D")], DAY
    )
    assert [c.member_txn_ids for c in cases] == [("txn_a",), ("txn_b",)]


def test_window_edge_is_included_and_past_it_is_not():
    on_edge = alert("txn_b", 2, ts="2022-09-16 10:00:00")
    past_edge = alert("txn_c", 3, ts="2022-09-16 10:00:01")
    first = alert("txn_a", 1)
    assert len(group_into_cases([first, on_edge], DAY)) == 1
    assert len(group_into_cases([first, past_edge], DAY)) == 2


def test_links_are_transitive():
    cases = group_into_cases(
        [
            alert("txn_a", 1, "1|A", "2|B"),
            alert("txn_b", 2, "2|B", "3|C"),
            alert("txn_c", 3, "3|C", "4|D"),
        ],
        DAY,
    )
    assert cases[0].member_txn_ids == ("txn_a", "txn_b", "txn_c")


def test_case_size_cap_starts_a_new_case_for_alerts_that_do_not_fit():
    alerts = [alert(f"txn_{i}", i) for i in range(1, 6)]
    cases = group_into_cases(alerts, DAY, max_size=2)
    assert all(len(c.member_txn_ids) <= 2 for c in cases)
    assert sorted(t for c in cases for t in c.member_txn_ids) == sorted(
        a.txn_id for a in alerts
    )
    assert cases[0].member_txn_ids == ("txn_1", "txn_2")


def test_grouping_ignores_input_order():
    alerts = [
        alert(f"txn_{i}", i, sender=f"{i % 3}|A", receiver=f"{i % 4}|B")
        for i in range(1, 30)
    ]
    assert group_into_cases(alerts, DAY) == group_into_cases(
        list(reversed(alerts)), DAY
    )


def test_queue_frame_conversion_drops_fractional_seconds():
    frame = pd.DataFrame(
        [
            {
                "transaction_id": "txn_a",
                "source_account_key": "1|A",
                "target_account_key": "2|B",
                "transaction_timestamp": "2022-09-14 10:00:00.000",
                "risk_rank_day": "7",
            }
        ]
    )
    assert queue_alerts_from_frame(frame) == [alert("txn_a", 7)]


# --- The code-only brief, with the tools replaced


def codes(result, key="findings"):
    return [item["code"] for item in result[key]]


def test_brief_case_returns_the_standard_result(monkeypatch):
    stub_tools(monkeypatch)
    result = baseline.brief_case(None, case_of("txn_a", "txn_b"))

    assert result["status"] == "ok"
    assert result["txn_id"] == "txn_a"
    assert result["case_id"] == "case_txn_a"
    assert result["fallback"] is False
    assert codes(result) == [
        "PASS_THROUGH",
        "NEIGHBOURS_HIGH_RISK",
        "REPEAT_ACCOUNT_ALERTS",
    ]
    assert codes(result, "gaps") == []
    assert codes(result, "next_checks") == [
        "CHECK_ONWARD_ACCOUNTS",
        "CHECK_SOURCE_ACCOUNTS",
        "REVIEW_RELATED_ALERTS",
    ]
    assert result["summary"].startswith("The fixed rules found:")
    assert [m["alert"]["txn_id"] for m in result["members"]] == ["txn_a", "txn_b"]
    assert [m["txn_id"] for m in result["money_flow"]] == ["txn_a", "txn_b"]
    assert result["stats"]["model"] == baseline.MODEL_NAME
    assert result["stats"]["tokens_in"] == 0
    assert result["stats"]["fallback"] is False
    assert all(step["actor"] == "code" for step in result["trace"])
    assert [s["step"] for s in result["trace"]] == list(range(len(result["trace"])))
    # Five tool calls per member (alert, graph, two activity, neighbours), and one related-alerts call.
    assert len(result["trace"]) == 11
    json.dumps(result)


def test_brief_case_runs_the_activity_tool_for_both_sides_of_every_member(monkeypatch):
    calls = stub_tools(monkeypatch)
    baseline.brief_case(None, case_of("txn_a", "txn_b"))
    sides = [(c[1], c[2]) for c in calls if c[0] == "get_account_activity"]
    assert sides == [
        ("txn_a", "sender"),
        ("txn_a", "receiver"),
        ("txn_b", "sender"),
        ("txn_b", "receiver"),
    ]


def test_every_finding_s1_makes_passes_the_validator(monkeypatch):
    stub_tools(monkeypatch, fan=6)
    result = baseline.brief_case(None, case_of("txn_a", "txn_b"))
    assert "FAN_OUT" in codes(result)

    evidence = RunEvidence(
        representative="txn_a",
        members=("txn_a", "txn_b"),
        txn_dates={"txn_a": "2022-09-14", "txn_b": "2022-09-14"},
    )
    for step in result["trace"]:
        evidence.record(step["tool"], step["result"])
    validated = validate_brief(
        {
            "findings": [
                {"code": f["code"], "evidence_ids": f["evidence_ids"]}
                for f in result["findings"]
            ],
            "next_checks": codes(result, "next_checks"),
            "summary": result["summary"],
        },
        evidence,
    )
    assert validated.errors == ()


def test_s1_cites_a_limited_number_of_ids_per_finding(monkeypatch):
    stub_tools(monkeypatch, fan=30)
    result = baseline.brief_case(None, case_of("txn_a"))
    fan_out = next(f for f in result["findings"] if f["code"] == "FAN_OUT")
    assert len(fan_out["evidence_ids"]) == 10


def test_a_case_with_no_payments_has_a_gap_and_no_payment_findings(monkeypatch):
    stub_tools(monkeypatch)
    monkeypatch.setattr(
        tools,
        "get_case_graph",
        lambda data, alert, window_h, as_of=None: graph_result(
            alert["txn_id"], downstream=(), upstream=()
        ),
    )
    result = baseline.brief_case(None, case_of("txn_a"))
    # The stubbed related-alerts tool still returns an alert, which does not depend on payments.
    assert codes(result) == ["REPEAT_ACCOUNT_ALERTS"]
    assert codes(result, "gaps") == ["NO_GRAPH_AVAILABLE"]
    assert result["case_id"] is None
    assert result["money_flow"][0]["no_graph"] is True


def test_a_truncated_window_is_a_gap_not_a_missing_finding(monkeypatch):
    stub_tools(monkeypatch, truncated=True)
    result = baseline.brief_case(None, case_of("txn_a"))
    assert codes(result, "gaps") == ["TRUNCATED_WINDOW", "SHORT_OBSERVATION"]
    assert "PASS_THROUGH" in codes(result)
    assert "RECHECK_AFTER_MORE_DATA" in codes(result, "next_checks")


def test_s1_gives_the_same_brief_every_time(monkeypatch):
    stub_tools(monkeypatch)

    def stable(result):
        return json.dumps(
            {k: v for k, v in result.items() if k not in ("stats", "trace")},
            sort_keys=True,
        )

    case = case_of("txn_a", "txn_b")
    assert stable(baseline.brief_case(None, case)) == stable(
        baseline.brief_case(None, case)
    )


def test_brief_day_uses_an_end_of_day_cutoff(monkeypatch):
    calls = stub_tools(monkeypatch)
    frame = pd.DataFrame(
        [
            {
                "transaction_id": "txn_a",
                "source_account_key": "1|A",
                "target_account_key": "2|B",
                "transaction_timestamp": "2022-09-14 10:00:00",
                "risk_rank_day": "1",
            }
        ]
    )
    monkeypatch.setattr(tools, "load_day_queue", lambda data, day, top_n: frame)
    baseline.brief_day(None, DAY)
    assert calls.as_of_seen
    assert set(calls.as_of_seen) == {"2022-09-14 23:59:59"}


def test_missing_queue_alert_raises(monkeypatch):
    stub_tools(monkeypatch, found=False)
    with pytest.raises(ValueError, match="not found"):
        baseline.brief_case(None, case_of("txn_a"))


# --- Review list: BQ1's order, with the brief attached


def rows_for(representative, finding_codes=(), *, members=()):
    result = {
        "status": "ok",
        "txn_id": representative,
        "txn_date": DAY,
        "case_id": f"case_{representative}" if members else None,
        "summary": "A summary.",
        "findings": [{"code": c} for c in finding_codes],
        "gaps": [{"code": "TRUNCATED_WINDOW"}],
        "next_checks": [],
        "stats": {
            "fallback": False,
            "repair_used": False,
            "steps": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "latency_ms": 0,
            "model": "test",
        },
    }
    return build_decision_rows(
        result, run_id="r", created_at="t", member_txn_ids=members
    )


QUEUE = [f"txn_{c}" for c in "abcdef"]


def ids(review):
    return [item["txn_id"] for item in review]


def test_no_rows_keeps_the_bq1_order_and_has_no_briefs():
    review = build_review_list(QUEUE, [])
    assert ids(review) == QUEUE
    assert [i["position"] for i in review] == [1, 2, 3, 4, 5, 6]
    assert not any(i["has_brief"] for i in review)
    assert review[0]["role"] is None and review[0]["finding_codes"] == []


def test_briefs_never_change_the_order():
    rows = rows_for("txn_c", ["PASS_THROUGH"], members=("txn_e",)) + rows_for(
        "txn_a", []
    )
    review = build_review_list(QUEUE, rows)
    assert ids(review) == QUEUE


def test_a_member_row_shows_its_representatives_brief():
    rows = rows_for("txn_c", ["PASS_THROUGH", "FAN_OUT"], members=("txn_e",))
    by_id = {i["txn_id"]: i for i in build_review_list(QUEUE, rows)}
    assert by_id["txn_c"]["role"] == "representative"
    assert by_id["txn_e"]["role"] == "member"
    assert by_id["txn_e"]["representative_txn_id"] == "txn_c"
    assert by_id["txn_e"]["case_id"] == "case_txn_c"
    assert by_id["txn_e"]["finding_codes"] == ["PASS_THROUGH", "FAN_OUT"]
    assert by_id["txn_e"]["gap_codes"] == ["TRUNCATED_WINDOW"]
    assert by_id["txn_e"]["has_brief"] is True
    assert by_id["txn_b"]["has_brief"] is False


def test_an_alert_with_no_findings_still_has_a_brief():
    by_id = {i["txn_id"]: i for i in build_review_list(QUEUE, rows_for("txn_b", []))}
    assert by_id["txn_b"]["has_brief"] is True and by_id["txn_b"]["finding_codes"] == []


def test_no_alert_ever_leaves_the_list_or_appears_twice():
    rows = rows_for("txn_a", ["FAN_IN"], members=("txn_b",)) + rows_for("txn_c", [])
    assert sorted(ids(build_review_list(QUEUE, rows))) == sorted(QUEUE)


def test_a_row_outside_the_queue_is_ignored():
    assert ids(build_review_list(QUEUE, rows_for("txn_z", ["FAN_IN"]))) == QUEUE
