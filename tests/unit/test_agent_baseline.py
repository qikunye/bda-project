import json

import pandas as pd
import pytest

from src import config
from src.agent import baseline, tools
from src.agent.baseline import MemberEvidence, Settings, decide_case
from src.agent.cases import Case, QueueAlert, group_into_cases, queue_alerts_from_frame
from src.agent.decisions import build_decision_rows
from src.agent.review_list import build_review_list

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


# --- Fixed rules


def neighbour(txn_id, rank, *, scored=True):
    return {
        "txn_id": txn_id,
        "txn_date": DAY,
        "scored": scored,
        "risk_score": 0.5 if scored else None,
        "risk_rank_global": rank,
        "risk_rank_day": rank,
        "in_queue": rank is not None,
        "in_day_top_k": rank is not None and rank <= config.AGENT_TOP_K,
        "has_reasons": False,
    }


def evidence(txn_id, neighbours=(), *, no_graph=None, truncated=False, reasons=True):
    items = list(neighbours)
    return MemberEvidence(
        alert={
            "found": True,
            "txn_id": txn_id,
            "reasons": [{"feature": "x"}] if reasons else None,
        },
        graph={
            "no_graph": (not items) if no_graph is None else no_graph,
            "window_truncated": truncated,
        },
        neighbours={"neighbours": items, "ids": [n["txn_id"] for n in items]},
    )


def case_of(*members):
    return Case(f"case_{members[0]}", DAY, members[0], tuple(members))


def related(*txn_ids):
    return {
        "alerts": [{"txn_id": t} for t in txn_ids],
        "top_k": config.AGENT_TOP_K,
        "window_h": 48,
        "ids": list(txn_ids),
    }


def codes(decision):
    return [c["code"] for c in decision["claims"]]


@pytest.mark.parametrize("rank", [501, 5000, None])
def test_neighbour_outside_the_top_k_does_not_raise(rank):
    decision = decide_case(
        case_of("txn_a"), [evidence("txn_a", [neighbour("txn_n", rank)])], related()
    )
    assert decision["verdict"] == "not_sure"
    assert decision["confidence"] == "low"
    assert decision["note"]
    assert "promoted_txn_ids" not in decision


def test_unscored_neighbour_does_not_raise():
    decision = decide_case(
        case_of("txn_a"),
        [evidence("txn_a", [neighbour("txn_n", None, scored=False)])],
        related(),
    )
    assert decision["verdict"] == "not_sure"


def test_claims_cite_related_members_and_high_neighbours():
    decision = decide_case(
        case_of("txn_a", "txn_b"),
        [
            evidence("txn_a", [neighbour("txn_n", 900)]),
            evidence("txn_b", [neighbour("txn_h", 10)]),
        ],
        related("txn_b"),
    )
    assert decision["verdict"] == "raise"
    assert codes(decision) == ["REPEAT_ACCOUNT_ALERTS", "NEIGHBOURS_HIGH_RISK"]


def test_two_members_raise():
    decision = decide_case(
        case_of("txn_a", "txn_b"),
        [evidence("txn_a"), evidence("txn_b")],
        related("txn_b"),
    )
    assert decision["verdict"] == "raise"
    assert decision["case_id"] == "case_txn_a"
    assert decision["confidence"] == "medium"


def test_one_member_and_one_high_neighbour_raise():
    decision = decide_case(
        case_of("txn_a"), [evidence("txn_a", [neighbour("txn_h", 500)])], related()
    )
    assert decision["verdict"] == "raise"
    assert decision["case_id"] is None
    assert codes(decision) == ["NEIGHBOURS_HIGH_RISK"]


def test_member_is_not_counted_again_as_a_neighbour():
    decision = decide_case(
        case_of("txn_a", "txn_b"),
        [evidence("txn_a", [neighbour("txn_b", 2)]), evidence("txn_b")],
        related("txn_b"),
    )
    assert "NEIGHBOURS_HIGH_RISK" not in codes(decision)


def test_no_payments_means_not_sure_with_low_confidence():
    decision = decide_case(case_of("txn_a"), [evidence("txn_a")], related())
    assert decision["verdict"] == "not_sure"
    assert decision["confidence"] == "low"
    assert codes(decision) == ["NO_GRAPH_AVAILABLE"]


def test_s1_never_says_dont_raise():
    for items in ([], [neighbour("txn_n", 900)], [neighbour("txn_h", 10)]):
        decision = decide_case(case_of("txn_a"), [evidence("txn_a", items)], related())
        assert decision["verdict"] != "dont_raise"


def test_truncated_window_adds_a_claim_and_keeps_the_confidence():
    decision = decide_case(
        case_of("txn_a"),
        [evidence("txn_a", [neighbour("txn_h", 10)], truncated=True)],
        related(),
    )
    assert decision["verdict"] == "raise"
    assert decision["confidence"] == "medium"
    assert "TRUNCATED_WINDOW" in codes(decision)


def test_missing_reasons_add_a_claim():
    decision = decide_case(
        case_of("txn_a"), [evidence("txn_a", reasons=False)], related()
    )
    assert "NO_REASONS_AVAILABLE" in codes(decision)


def test_rules_read_the_thresholds_from_settings():
    decision = decide_case(
        case_of("txn_a", "txn_b"),
        [evidence("txn_a"), evidence("txn_b")],
        related(),
        Settings(escalate_min_high_ranked=3),
    )
    assert decision["verdict"] == "not_sure"


def test_decide_case_gives_the_same_decision_every_time():
    args = (
        case_of("txn_a", "txn_b"),
        [
            evidence("txn_a", [neighbour("txn_n2", 700), neighbour("txn_n1", 600)]),
            evidence("txn_b", [neighbour("txn_n1", 600)]),
        ],
        related("txn_b"),
    )
    assert decide_case(*args) == decide_case(*args)


# --- Full case, with the tools replaced


def stub_tools(monkeypatch, *, ranks):
    seen_as_of: list = []

    def get_alert(data, txn_id, txn_date, top_k):
        return {
            "found": True,
            "txn_id": txn_id,
            "txn_date": txn_date,
            "ts": "2022-09-14 10:00:00",
            "sender": f"1|{txn_id[-1].upper()}",
            "receiver": "2|B",
            "reasons": None,
            "ids": [txn_id, f"1|{txn_id[-1].upper()}", "2|B"],
        }

    def get_case_graph(data, found, window_h, as_of=None):
        seen_as_of.append(as_of)
        edges = [
            {
                "txn_id": t,
                "txn_date": DAY,
                "gap_minutes": 15,
                "sender": "2|B",
                "receiver": "3|C",
            }
            for t in ranks
        ]
        return {
            "txn_id": found["txn_id"],
            "window_h": window_h,
            "upstream": [],
            "downstream": edges,
            "n_upstream": 0,
            "n_downstream": len(edges),
            "no_graph": not edges,
            "window_truncated": False,
            "horizon_end": config.AGENT_DATA_HORIZON,
            "ids": [found["txn_id"]] + list(ranks),
        }

    def get_neighbour_scores(data, txn_ids, txn_dates, top_k):
        items = [neighbour(t, ranks[t]) for t in txn_ids]
        return {"neighbours": items, "ids": list(txn_ids)}

    def get_related_alerts(
        data, accounts, ts, window_h, exclude_txn_ids, top_k, as_of=None
    ):
        seen_as_of.append(as_of)
        return related("txn_b")

    monkeypatch.setattr(tools, "get_alert", get_alert)
    monkeypatch.setattr(tools, "get_case_graph", get_case_graph)
    monkeypatch.setattr(tools, "get_neighbour_scores", get_neighbour_scores)
    monkeypatch.setattr(tools, "get_related_alerts", get_related_alerts)
    return seen_as_of


def test_triage_day_uses_an_end_of_day_cutoff(monkeypatch):
    seen_as_of = stub_tools(monkeypatch, ranks={})
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
    baseline.triage_day(None, DAY)
    assert set(seen_as_of) == {"2022-09-14 23:59:59"}


def test_triage_case_returns_the_standard_result(monkeypatch):
    stub_tools(monkeypatch, ranks={"txn_n": 12})
    result = baseline.triage_case(None, case_of("txn_a", "txn_b"))

    assert result["status"] == "ok"
    assert result["txn_id"] == "txn_a"
    assert result["decision"]["verdict"] == "raise"
    assert [m["alert"]["txn_id"] for m in result["members"]] == ["txn_a", "txn_b"]
    assert [m["graph"]["txn_id"] for m in result["members"]] == ["txn_a", "txn_b"]
    assert result["stats"]["model"] == baseline.MODEL_NAME
    assert result["stats"]["tokens_in"] == 0
    assert result["stats"]["fallback"] is False
    assert all(step["actor"] == "code" for step in result["trace"])
    assert [s["step"] for s in result["trace"]] == list(range(len(result["trace"])))
    # 3 tools per member, and one related-alerts call.
    assert len(result["trace"]) == 7
    assert any("1 of 1 scored" in c["sentence"] for c in result["rendered_claims"])
    json.dumps(result)

    known = (
        set(result["alert"]["ids"])
        | set(result["neighbours"]["ids"])
        | set(result["related"]["ids"])
    )
    for claim in result["decision"]["claims"]:
        assert set(claim["evidence_ids"]) <= known


def test_missing_queue_alert_raises(monkeypatch):
    stub_tools(monkeypatch, ranks={})
    monkeypatch.setattr(
        tools, "get_alert", lambda data, txn_id, txn_date, top_k: {"found": False}
    )
    with pytest.raises(ValueError, match="not found"):
        baseline.triage_case(None, case_of("txn_a"))


# --- Review list


def rows_for(representative, verdict, *, members=()):
    result = {
        "status": "ok",
        "txn_id": representative,
        "txn_date": DAY,
        "decision": {
            "verdict": verdict,
            "confidence": "medium",
            "case_id": f"case_{representative}",
            "claims": [],
            "note": None,
        },
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


def test_no_decisions_keeps_the_bq1_order():
    review = build_review_list(QUEUE, [])
    assert ids(review) == QUEUE
    assert [i["position"] for i in review] == [1, 2, 3, 4, 5, 6]
    assert review[0]["bq1_position"] == 1
    assert review[0]["source"] == "queue" and review[0]["verdict"] is None


def test_a_raised_case_moves_up_as_a_group():
    rows = rows_for("txn_c", "raise", members=("txn_e",))
    review = build_review_list(QUEUE, rows)
    assert ids(review) == ["txn_c", "txn_e", "txn_a", "txn_b", "txn_d", "txn_f"]
    assert review[1]["source"] == "raised"
    assert review[1]["representative_txn_id"] == "txn_c"
    assert review[1]["bq1_position"] == 5


def test_case_with_the_better_representative_comes_first():
    rows = rows_for("txn_d", "raise") + rows_for("txn_b", "raise", members=("txn_f",))
    assert ids(build_review_list(QUEUE, rows))[:3] == ["txn_b", "txn_f", "txn_d"]


def test_dont_raise_moves_to_the_bottom_with_its_members():
    rows = rows_for("txn_a", "dont_raise", members=("txn_c",))
    review = build_review_list(QUEUE, rows)
    assert ids(review) == ["txn_b", "txn_d", "txn_e", "txn_f", "txn_a", "txn_c"]
    assert review[-1]["source"] == "not_raised"


def test_not_sure_stays_in_rank_order():
    rows = rows_for("txn_b", "not_sure")
    review = build_review_list(QUEUE, rows)
    assert ids(review) == QUEUE
    assert review[1]["verdict"] == "not_sure"
    assert review[1]["source"] == "queue"


def test_raised_then_not_sure_then_not_raised():
    rows = (
        rows_for("txn_a", "dont_raise")
        + rows_for("txn_b", "not_sure")
        + rows_for("txn_e", "raise")
    )
    assert ids(build_review_list(QUEUE, rows)) == [
        "txn_e",
        "txn_b",
        "txn_c",
        "txn_d",
        "txn_f",
        "txn_a",
    ]


def test_no_alert_ever_leaves_the_list_or_appears_twice():
    rows = (
        rows_for("txn_a", "dont_raise", members=("txn_b",))
        + rows_for("txn_c", "raise", members=("txn_d",))
        + rows_for("txn_e", "not_sure")
    )
    listed = ids(build_review_list(QUEUE, rows))
    assert sorted(listed) == sorted(QUEUE)


def test_a_row_outside_the_queue_is_ignored():
    assert ids(build_review_list(QUEUE, rows_for("txn_z", "raise"))) == QUEUE
