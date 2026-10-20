import json

import pandas as pd
import pytest

from src import config
from src.agent import tools
from src.agent.data import AgentData
from src.agent.sql import LabelLeakError
from src.aws.athena import QueryStats

ALERT_ID = "txn_aaa111"
SENDER = "10|A1"
RECEIVER = "20|B2"

REASON_COLUMNS = {
    "feature_1": "payment_format",
    "reason_1": "↑ Paid by ACH",
    "contribution_1": "3.859",
    "feature_2": "pair_prior_cnt",
    "reason_2": "↑ First payment between these two accounts",
    "contribution_2": "2.249",
    "feature_3": "hour_of_day",
    "reason_3": "↓ Paid at 14:00",
    "contribution_3": "-0.512",
}
NO_REASONS = {key: None for key in REASON_COLUMNS}


def alert_frame(ts="2022-09-14 10:00:00.000", rank_day="3", reasons=REASON_COLUMNS):
    return pd.DataFrame(
        [
            {
                "transaction_id": ALERT_ID,
                "source_account_key": SENDER,
                "target_account_key": RECEIVER,
                "transaction_timestamp": ts,
                "risk_score": "0.987654",
                "risk_rank_global": "250",
                "risk_rank_day": rank_day,
                "amount_paid": "1500.250000",
                "payment_currency": "US Dollar",
                "payment_format": "ACH",
                **reasons,
            }
        ]
    )


def edge(
    role,
    txn_id,
    sender,
    receiver,
    gap,
    amount="100.000000",
    currency="US Dollar",
    ts="2022-09-14 09:00:00.000",
):
    return {
        "edge_role": role,
        "transaction_id": txn_id,
        "source_account_key": sender,
        "target_account_key": receiver,
        "transaction_timestamp": ts,
        "gap_minutes": str(gap),
        "amount_paid": amount,
        "payment_currency": currency,
        "payment_format": "ACH",
    }


GRAPH_COLUMNS = list(edge("ALERT", ALERT_ID, SENDER, RECEIVER, 0))


class FakeAthena:
    """Answers each agent statement by matching its text; counts real (uncached) calls."""

    def __init__(self, alert=None, graph=None, scores=None, queues=None):
        self.alert = alert if alert is not None else alert_frame()
        self.graph = graph if graph is not None else pd.DataFrame(columns=GRAPH_COLUMNS)
        self.scores = scores if scores is not None else pd.DataFrame()
        self.queues = queues or {}
        self.calls = []

    def __call__(self, statement):
        self.calls.append(statement)
        stats = QueryStats("q", 1_000_000, 10)
        if "edge_role" in statement:
            return self.graph, stats
        if "has_reasons" in statement:
            return self.scores, stats
        if "ORDER BY risk_rank_day" in statement:
            day = statement.split("txn_date = DATE '")[1][:10]
            return self.queues.get(day, pd.DataFrame(columns=["transaction_id"])), stats
        return self.alert, stats


def data_for(**kwargs):
    fake = FakeAthena(**kwargs)
    return AgentData(runner=fake), fake


# --- get_alert ---------------------------------------------------------------------------


def test_get_alert_returns_typed_fields_reasons_and_ids():
    data, _ = data_for()
    alert = tools.get_alert(data, ALERT_ID, "2022-09-14")

    assert alert["found"] is True
    assert alert["ts"] == "2022-09-14 10:00:00"
    assert alert["amount"] == 1500.25
    assert alert["risk_score"] == 0.9877
    assert alert["risk_rank_global"] == 250
    assert alert["risk_rank_day"] == 3
    assert alert["in_queue"] is True
    assert alert["in_day_top_k"] is True
    assert alert["reasons"][0] == {
        "feature": "payment_format",
        "sentence": "Paid by ACH",
        "direction": "up",
        "contribution": 3.859,
    }
    assert alert["reasons"][2]["direction"] == "down"
    assert alert["ids"] == [ALERT_ID, SENDER, RECEIVER]
    json.dumps(alert)


def test_get_alert_without_reasons_returns_null():
    data, _ = data_for(alert=alert_frame(reasons=NO_REASONS))
    assert tools.get_alert(data, ALERT_ID, "2022-09-14")["reasons"] is None


def test_get_alert_validation_row_is_not_in_queue():
    data, _ = data_for(alert=alert_frame(rank_day=None))
    alert = tools.get_alert(data, ALERT_ID, "2022-09-14")
    assert alert["in_queue"] is False
    assert alert["in_day_top_k"] is False


def test_get_alert_outside_scored_days_does_not_query():
    data, fake = data_for()
    alert = tools.get_alert(data, ALERT_ID, "2022-09-03")
    assert alert == {
        "found": False,
        "txn_id": ALERT_ID,
        "txn_date": "2022-09-03",
        "ids": [],
    }
    assert fake.calls == []


def test_get_alert_not_found():
    data, _ = data_for(alert=pd.DataFrame(columns=alert_frame().columns))
    assert tools.get_alert(data, ALERT_ID, "2022-09-14")["found"] is False


# --- get_case_graph ----------------------------------------------------------------------


def graph_frame(rows):
    return pd.DataFrame(
        [edge("ALERT", ALERT_ID, SENDER, RECEIVER, 0)] + rows, columns=GRAPH_COLUMNS
    )


def get_graph(rows, ts="2022-09-14 10:00:00.000", window_h=48, **kwargs):
    data, _ = data_for(alert=alert_frame(ts=ts), graph=graph_frame(rows))
    alert = tools.get_alert(data, ALERT_ID, ts[:10])
    return tools.get_case_graph(data, alert, window_h, **kwargs)


def test_case_graph_splits_sorts_and_counts():
    graph = get_graph(
        [
            edge("UPSTREAM", "txn_u2", "30|C", SENDER, 120),
            edge("UPSTREAM", "txn_u1", "31|D", SENDER, 30),
            edge("UPSTREAM", "txn_u3", "31|D", SENDER, 600),
            edge(
                "DOWNSTREAM",
                "txn_d1",
                RECEIVER,
                "40|E",
                15,
                ts="2022-09-14 10:15:00.000",
            ),
        ]
    )
    assert [e["txn_id"] for e in graph["upstream"]] == ["txn_u1", "txn_u2", "txn_u3"]
    assert graph["n_upstream"] == 3
    assert graph["n_upstream_accounts"] == 2
    assert graph["n_downstream"] == 1
    assert graph["downstream"][0] == {
        "txn_id": "txn_d1",
        "txn_date": "2022-09-14",
        "ts": "2022-09-14 10:15:00",
        "sender": RECEIVER,
        "receiver": "40|E",
        "amount": 100.0,
        "currency": "US Dollar",
        "payment_format": "ACH",
        "gap_minutes": 15,
    }
    assert graph["no_graph"] is False
    assert graph["edges_capped"] is False
    json.dumps(graph)


def test_case_graph_totals_are_per_currency():
    graph = get_graph(
        [
            edge(
                "UPSTREAM",
                "txn_u1",
                "30|C",
                SENDER,
                10,
                amount="100.10",
                currency="Euro",
            ),
            edge(
                "UPSTREAM", "txn_u2", "30|C", SENDER, 20, amount="0.20", currency="Euro"
            ),
            edge(
                "UPSTREAM", "txn_u3", "30|C", SENDER, 30, amount="5000", currency="Yen"
            ),
        ]
    )
    assert graph["per_currency_totals"] == {
        "upstream": {"Euro": 100.3, "Yen": 5000.0},
        "downstream": {},
    }


def test_case_graph_caps_edges_and_cites_only_shown_ones():
    rows = [edge("UPSTREAM", f"txn_u{i}", f"5{i}|X", SENDER, i) for i in range(5)]
    graph = get_graph(rows, max_edges=2)
    assert [e["txn_id"] for e in graph["upstream"]] == ["txn_u0", "txn_u1"]
    assert graph["n_upstream"] == 5
    assert graph["edges_capped"] is True
    assert "txn_u4" not in graph["ids"]
    assert "50|X" in graph["ids"]
    assert "54|X" not in graph["ids"]
    assert graph["per_currency_totals"]["upstream"] == {"US Dollar": 500.0}


def test_case_graph_without_edges_flags_no_graph():
    graph = get_graph([])
    assert graph["no_graph"] is True
    assert graph["upstream"] == [] and graph["downstream"] == []
    assert graph["ids"] == [ALERT_ID, SENDER, RECEIVER]


@pytest.mark.parametrize(
    ("ts", "window_h", "truncated"),
    [
        ("2022-09-14 10:00:00.000", 48, False),
        ("2022-09-14 23:59:59.000", 48, False),
        ("2022-09-15 00:00:00.000", 48, True),
        ("2022-09-15 10:00:00.000", 24, False),
        ("2022-09-16 00:00:01.000", 24, True),
        ("2022-09-16 20:00:00.000", 6, True),
    ],
)
def test_window_truncated_follows_data_horizon(ts, window_h, truncated):
    assert get_graph([], ts=ts, window_h=window_h)["window_truncated"] is truncated


def test_case_graph_requires_found_alert():
    data, _ = data_for()
    with pytest.raises(ValueError):
        tools.get_case_graph(data, {"found": False}, 48)


# --- get_neighbour_scores ----------------------------------------------------------------


def test_neighbour_scores_marks_unscored_validation_and_missing():
    scores = pd.DataFrame(
        [
            {
                "transaction_id": "txn_n1",
                "risk_score": "0.91",
                "risk_rank_global": "900",
                "risk_rank_day": "12",
                "has_reasons": "true",
            },
            {
                "transaction_id": "txn_n2",
                "risk_score": "0.40",
                "risk_rank_global": "90000",
                "risk_rank_day": "8000",
                "has_reasons": "false",
            },
            {
                "transaction_id": "txn_n3",
                "risk_score": "0.70",
                "risk_rank_global": "5000",
                "risk_rank_day": None,
                "has_reasons": "false",
            },
        ]
    )
    data, fake = data_for(scores=scores)
    result = tools.get_neighbour_scores(
        data,
        ["txn_n1", "txn_n2", "txn_n3", "txn_old", "txn_gone", "txn_n1"],
        [
            "2022-09-14",
            "2022-09-15",
            "2022-09-14",
            "2022-09-13",
            "2022-09-15",
            "2022-09-14",
        ],
    )
    by_id = {n["txn_id"]: n for n in result["neighbours"]}

    assert len(result["neighbours"]) == 5
    assert (
        by_id["txn_n1"]["in_day_top_k"] is True
        and by_id["txn_n1"]["has_reasons"] is True
    )
    assert (
        by_id["txn_n2"]["in_queue"] is True and by_id["txn_n2"]["in_day_top_k"] is False
    )
    assert by_id["txn_n3"]["scored"] is True and by_id["txn_n3"]["in_queue"] is False
    assert by_id["txn_old"]["scored"] is False
    assert by_id["txn_gone"]["scored"] is False
    assert result["ids"] == ["txn_n1", "txn_n2", "txn_n3", "txn_old", "txn_gone"]
    assert len(fake.calls) == 1
    assert "2022-09-13" not in fake.calls[0]


def test_neighbour_scores_only_unscored_dates_skip_query():
    data, fake = data_for()
    result = tools.get_neighbour_scores(data, ["txn_old"], ["2022-09-02"])
    assert result["neighbours"][0]["scored"] is False
    assert fake.calls == []


def test_neighbour_scores_rejects_mismatched_lists():
    data, _ = data_for()
    with pytest.raises(ValueError):
        tools.get_neighbour_scores(data, ["txn_a", "txn_b"], ["2022-09-14"])


# --- get_related_alerts ------------------------------------------------------------------


def queue_row(txn_id, sender, receiver, ts, rank):
    return {
        "transaction_id": txn_id,
        "source_account_key": sender,
        "target_account_key": receiver,
        "transaction_timestamp": ts,
        "risk_score": "0.9",
        "risk_rank_global": "1",
        "risk_rank_day": str(rank),
    }


def test_related_alerts_filters_accounts_window_and_anchor():
    queues = {
        "2022-09-14": pd.DataFrame(
            [
                queue_row(ALERT_ID, SENDER, RECEIVER, "2022-09-14 10:00:00.000", 1),
                queue_row("txn_r1", RECEIVER, "60|F", "2022-09-14 12:00:00.000", 4),
                queue_row("txn_r2", "61|G", SENDER, "2022-09-14 08:00:00.000", 2),
                queue_row("txn_other", "63|I", "64|J", "2022-09-14 10:00:00.000", 5),
            ]
        ),
        "2022-09-15": pd.DataFrame(
            [
                queue_row("txn_r3", "65|K", RECEIVER, "2022-09-15 09:00:00.000", 7),
                queue_row("txn_far", SENDER, "62|H", "2022-09-15 10:00:01.000", 3),
            ]
        ),
    }
    data, fake = data_for(queues=queues)
    result = tools.get_related_alerts(
        data, [SENDER, RECEIVER], "2022-09-14 10:00:00", 24, exclude_txn_ids=(ALERT_ID,)
    )

    assert [(a["txn_id"], a["account"], a["role"]) for a in result["alerts"]] == [
        ("txn_r2", SENDER, "receiver"),
        ("txn_r1", RECEIVER, "sender"),
        ("txn_r3", RECEIVER, "receiver"),
    ]
    assert result["ids"] == ["txn_r2", "txn_r1", "txn_r3", SENDER, RECEIVER]
    assert len(fake.calls) == 2  # 14 and 15 Sep; 13 Sep is not a scored day

    tools.get_related_alerts(data, [SENDER], "2022-09-14 11:00:00", 24)
    assert len(fake.calls) == 2  # day queues come from the cache


# --- AgentData ---------------------------------------------------------------------------


def test_cache_serves_repeats_and_logs_them():
    data, fake = data_for()
    tools.get_alert(data, ALERT_ID, "2022-09-14")
    tools.get_alert(data, ALERT_ID, "2022-09-14")
    assert len(fake.calls) == 1
    assert [entry.cached for entry in data.scan_log] == [False, True]
    assert data.bytes_scanned == 1_000_000


def test_result_with_label_column_is_rejected():
    leaked = alert_frame().assign(evaluation_is_laundering="1")
    data, _ = data_for(alert=leaked)
    with pytest.raises(LabelLeakError):
        tools.get_alert(data, ALERT_ID, "2022-09-14")


def test_forbidden_statement_is_rejected_before_running():
    data, fake = data_for()
    with pytest.raises(LabelLeakError):
        data.query("SELECT is_laundering FROM t", "bad")
    assert fake.calls == []


def test_tool_outputs_never_contain_forbidden_keys():
    data, _ = data_for(
        graph=graph_frame([edge("UPSTREAM", "txn_u1", "30|C", SENDER, 10)])
    )
    alert = tools.get_alert(data, ALERT_ID, "2022-09-14")
    outputs = [
        alert,
        tools.get_case_graph(data, alert, 48),
        tools.get_neighbour_scores(data, ["txn_x"], ["2022-09-02"]),
        tools.get_related_alerts(data, [SENDER], alert["ts"], 6),
    ]
    text = json.dumps(outputs)
    for column in config.AGENT_FORBIDDEN_COLUMNS:
        assert f'"{column}"' not in text
