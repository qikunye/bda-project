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

def alert_frame(ts="2022-09-14 10:00:00.000", rank_day="3"):
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


def activity(
    direction,
    txn_id,
    counterparty,
    offset,
    amount="100.000000",
    currency="US Dollar",
    ts="2022-09-14 10:00:00.000",
):
    return {
        "direction": direction,
        "transaction_id": txn_id,
        "counterparty_key": counterparty,
        "transaction_timestamp": ts,
        "offset_minutes": str(offset),
        "amount_paid": amount,
        "payment_currency": currency,
        "payment_format": "ACH",
    }


ACTIVITY_COLUMNS = list(activity("in", "txn_x", "30|C", 0))
HISTORY_COLUMNS = [
    "txn_date",
    "direction",
    "payment_currency",
    "n_payments",
    "total_amount",
]


def history_row(day, direction, n, total, currency="US Dollar"):
    return {
        "txn_date": day,
        "direction": direction,
        "payment_currency": currency,
        "n_payments": str(n),
        "total_amount": total,
    }


class FakeAthena:
    """Answers each agent statement by matching its text; counts real (uncached) calls."""

    def __init__(
        self, alert=None, graph=None, scores=None, queues=None, activity=None, history=None
    ):
        self.alert = alert if alert is not None else alert_frame()
        self.activity = (
            activity if activity is not None else pd.DataFrame(columns=ACTIVITY_COLUMNS)
        )
        self.history = (
            history if history is not None else pd.DataFrame(columns=HISTORY_COLUMNS)
        )
        self.graph = graph if graph is not None else pd.DataFrame(columns=GRAPH_COLUMNS)
        self.scores = scores if scores is not None else pd.DataFrame()
        self.queues = queues or {}
        self.calls = []

    def __call__(self, statement):
        self.calls.append(statement)
        stats = QueryStats("q", 1_000_000, 10)
        if "edge_role" in statement:
            return self.graph, stats
        if "offset_minutes" in statement:
            return self.activity, stats
        if "n_payments" in statement:
            return self.history, stats
        if "txn_date IN (" in statement:
            return self.scores, stats
        if "ORDER BY risk_rank_day" in statement:
            day = statement.split("txn_date = DATE '")[1][:10]
            return self.queues.get(day, pd.DataFrame(columns=["transaction_id"])), stats
        return self.alert, stats


def data_for(**kwargs):
    fake = FakeAthena(**kwargs)
    return AgentData(runner=fake), fake


# --- get_alert ---------------------------------------------------------------------------


def test_get_alert_returns_typed_fields_and_ids():
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
    assert alert["ids"] == [ALERT_ID, SENDER, RECEIVER]
    json.dumps(alert)


def test_get_alert_never_returns_bq1_reasons():
    # The agent does not see the model's reasons, so neither the field nor its columns exist.
    data, fake = data_for()
    alert = tools.get_alert(data, ALERT_ID, "2022-09-14")
    assert "reasons" not in alert
    assert "feature_1" not in fake.calls[0] and "reason_1" not in fake.calls[0]


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


def test_a_cutoff_is_reported_but_does_not_truncate_the_window():
    # Only the end of the dataset truncates. A run's cutoff shows in hours_observed_after_alert.
    graph = get_graph([], as_of="2022-09-14 23:59:59")
    assert graph["window_truncated"] is False
    assert graph["horizon_end"] == "2022-09-14 23:59:59"
    assert graph["hours_observed_after_alert"] == 14.0


def test_a_cutoff_does_not_hide_that_the_window_passes_the_end_of_the_data():
    graph = get_graph([], ts="2022-09-16 20:00:00.000", as_of="2022-09-16 23:59:59")
    assert graph["window_truncated"] is True


def test_a_cutoff_past_the_window_does_not_truncate():
    graph = get_graph([], window_h=6, as_of="2022-09-14 23:59:59")
    assert graph["window_truncated"] is False


def test_a_cutoff_cannot_extend_past_the_data_horizon():
    graph = get_graph([], as_of="2099-01-01 00:00:00")
    assert graph["horizon_end"] == config.AGENT_DATA_HORIZON


@pytest.mark.parametrize(
    ("ts", "window_h", "as_of", "hours"),
    [
        ("2022-09-14 10:00:00.000", 48, None, 48.0),  # Window fits in the data.
        ("2022-09-16 12:00:00.000", 48, None, 12.0),  # Data ends 12 h after the alert.
        ("2022-09-14 10:00:00.000", 48, "2022-09-14 23:59:59", 14.0),  # Day-end cutoff.
        ("2022-09-14 10:00:00.000", 6, "2022-09-14 23:59:59", 6.0),  # Capped at the window.
    ],
)
def test_hours_observed_after_alert_is_capped_at_window_and_data(
    ts, window_h, as_of, hours
):
    graph = get_graph([], ts=ts, window_h=window_h, as_of=as_of)
    assert graph["hours_observed_after_alert"] == pytest.approx(hours, abs=0.1)


def test_returned_to_source_finds_money_back_to_the_senders_sources():
    graph = get_graph(
        [
            edge("UPSTREAM", "txn_u_far", "30|C", SENDER, 300),
            edge("UPSTREAM", "txn_u_near", "30|C", SENDER, 60),
            edge("UPSTREAM", "txn_u_other", "31|D", SENDER, 90),
            edge("DOWNSTREAM", "txn_d_new", RECEIVER, "40|E", 10),
            edge("DOWNSTREAM", "txn_d_src", RECEIVER, "30|C", 20),
            edge("DOWNSTREAM", "txn_d_sender", RECEIVER, SENDER, 30),
        ]
    )
    assert graph["returned_to_source"] == [
        {
            "account": "30|C",
            "kind": "upstream_source",
            "downstream_txn_id": "txn_d_src",
            "source_txn_id": "txn_u_near",  # Closest upstream payment from that account.
            "gap_minutes": 20,
        },
        {
            "account": SENDER,
            "kind": "alert_sender",
            "downstream_txn_id": "txn_d_sender",
            "source_txn_id": ALERT_ID,
            "gap_minutes": 30,
        },
    ]
    for entry in graph["returned_to_source"]:  # Both IDs are citable.
        assert {entry["downstream_txn_id"], entry["source_txn_id"], entry["account"]} <= set(
            graph["ids"]
        )


def test_returned_to_source_is_empty_when_money_goes_elsewhere():
    graph = get_graph(
        [
            edge("UPSTREAM", "txn_u1", "30|C", SENDER, 60),
            edge("DOWNSTREAM", "txn_d1", RECEIVER, "40|E", 10),
        ]
    )
    assert graph["returned_to_source"] == []


def test_returned_to_source_checks_edges_beyond_the_display_cap():
    rows = [
        edge("DOWNSTREAM", f"txn_d{n:02d}", RECEIVER, f"4{n}|X", n) for n in range(1, 4)
    ] + [edge("DOWNSTREAM", "txn_d_back", RECEIVER, SENDER, 100)]
    graph = get_graph(rows, max_edges=2)
    assert graph["edges_capped"] is True
    assert "txn_d_back" not in [e["txn_id"] for e in graph["downstream"]]
    assert [r["downstream_txn_id"] for r in graph["returned_to_source"]] == ["txn_d_back"]
    assert "txn_d_back" in graph["ids"]


def test_returned_to_source_is_limited():
    rows = [
        edge("DOWNSTREAM", f"txn_d{n:02d}", RECEIVER, SENDER, n)
        for n in range(1, config.AGENT_MAX_RETURNED_TO_SOURCE + 5)
    ]
    graph = get_graph(rows)
    assert len(graph["returned_to_source"]) == config.AGENT_MAX_RETURNED_TO_SOURCE


def test_flow_reports_quick_onward_share_of_the_alert_amount_and_gaps_per_currency():
    # The alert is 1,500.25 US Dollar.
    graph = get_graph(
        [
            edge("UPSTREAM", "txn_u1", "30|C", SENDER, 60, amount="400.000000"),
            edge("UPSTREAM", "txn_u2", "31|D", SENDER, 90, amount="600.000000"),
            edge("DOWNSTREAM", "txn_d1", RECEIVER, "40|E", 15, amount="500.000000"),
            edge("DOWNSTREAM", "txn_d2", RECEIVER, "41|F", 45, amount="500.000000"),
            edge("DOWNSTREAM", "txn_d3", RECEIVER, "42|G", 120, amount="500.000000"),
            edge("UPSTREAM", "txn_ue", "32|H", SENDER, 70, amount="50.000000", currency="Euro"),
        ]
    )
    assert graph["flow"]["US Dollar"] == {
        "upstream_total": 1000.0,
        "downstream_total": 1500.0,
        "quick_onward_share": 1.0,  # 1,500 of the 1,500.25 alert, whatever the sender took in.
        "fastest_onward_gap_minutes": 15,
        "median_onward_gap_minutes": 45.0,
        "fastest_onward_txn_id": "txn_d1",
    }
    # Euro came in, but the alert is in dollars, so there is no share to compute.
    assert graph["flow"]["Euro"]["quick_onward_share"] is None
    assert graph["flow"]["Euro"]["fastest_onward_gap_minutes"] is None
    assert graph["flow"]["Euro"]["fastest_onward_txn_id"] is None
    assert "txn_d1" in graph["ids"]


def test_quick_onward_share_does_not_depend_on_what_the_sender_took_in():
    onward = edge("DOWNSTREAM", "txn_d1", RECEIVER, "40|E", 15, amount="750.125000")
    small = get_graph([edge("UPSTREAM", "txn_u1", "30|C", SENDER, 60, amount="10.000000"), onward])
    huge = get_graph([edge("UPSTREAM", "txn_u1", "30|C", SENDER, 60, amount="9000000.000000"), onward])
    none = get_graph([onward])
    shares = [g["flow"]["US Dollar"]["quick_onward_share"] for g in (small, huge, none)]
    assert shares == [0.5, 0.5, 0.5]


def test_quick_onward_share_counts_only_payments_within_the_pass_through_gap():
    # The alert is 1,500.25 US Dollar. A small quick payment, then a large one much later.
    limit = config.AGENT_PASS_THROUGH_MAX_GAP_MIN
    graph = get_graph(
        [
            edge("DOWNSTREAM", "txn_d1", RECEIVER, "40|E", 15, amount="150.050000"),
            edge("DOWNSTREAM", "txn_d2", RECEIVER, "41|F", limit, amount="150.000000"),
            edge("DOWNSTREAM", "txn_d3", RECEIVER, "42|G", limit + 1, amount="5000.000000"),
        ]
    )
    flow = graph["flow"]["US Dollar"]
    assert flow["quick_onward_share"] == 0.2  # 300.05 of 1,500.25; the later 5,000 does not count.
    assert flow["downstream_total"] == 5300.05  # Totals still cover the whole window.
    assert flow["fastest_onward_txn_id"] == "txn_d1"


def test_quick_onward_share_is_zero_when_nothing_went_on_within_the_gap():
    late = config.AGENT_PASS_THROUGH_MAX_GAP_MIN + 60
    graph = get_graph([edge("DOWNSTREAM", "txn_d1", RECEIVER, "40|E", late, amount="1500.250000")])
    assert graph["flow"]["US Dollar"]["quick_onward_share"] == 0.0
    assert graph["flow"]["US Dollar"]["fastest_onward_gap_minutes"] == late


def test_quick_onward_share_is_null_when_the_alert_has_no_amount():
    alert_row = alert_frame()
    alert_row["amount_paid"] = None
    data, _ = data_for(
        alert=alert_row,
        graph=graph_frame([edge("DOWNSTREAM", "txn_d1", RECEIVER, "40|E", 15)]),
    )
    alert = tools.get_alert(data, ALERT_ID, "2022-09-14")
    graph = tools.get_case_graph(data, alert, 48)
    assert graph["flow"]["US Dollar"]["quick_onward_share"] is None
    assert graph["flow"]["US Dollar"]["fastest_onward_gap_minutes"] == 15


def test_flow_is_empty_without_a_graph():
    assert get_graph([])["flow"] == {}
    assert get_graph([])["returned_to_source"] == []


def test_case_graph_requires_found_alert():
    data, _ = data_for()
    with pytest.raises(ValueError):
        tools.get_case_graph(data, {"found": False}, 48)


# --- get_account_activity ----------------------------------------------------------------


def get_activity(
    rows, history=(), side="sender", ts="2022-09-14 10:00:00.000", window_h=48, **kwargs
):
    data, fake = data_for(
        alert=alert_frame(ts=ts),
        activity=pd.DataFrame(rows, columns=ACTIVITY_COLUMNS),
        history=pd.DataFrame(list(history), columns=HISTORY_COLUMNS),
    )
    alert = tools.get_alert(data, ALERT_ID, ts[:10])
    return tools.get_account_activity(data, alert, side, window_h, **kwargs), fake


def test_account_activity_counts_and_totals_use_every_payment():
    result, _ = get_activity(
        [
            activity("in", "txn_i1", "30|C", -60, amount="400.000000"),
            activity("in", "txn_i2", "31|D", -30, amount="100.000000"),
            activity("in", "txn_i3", "30|C", -600, amount="50.000000", currency="Euro"),
            activity("out", "txn_o1", "40|E", 15),
            activity("out", "txn_o2", "40|E", 45),
            activity("out", "txn_o3", "41|F", 90),
        ]
    )
    assert result["account"] == SENDER and result["side"] == "sender"
    assert (result["n_incoming"], result["n_outgoing"]) == (3, 3)
    assert result["n_incoming_counterparties"] == 2
    assert result["n_outgoing_counterparties"] == 2
    assert result["per_currency_totals"] == {
        "incoming": {"Euro": 50.0, "US Dollar": 500.0},
        "outgoing": {"US Dollar": 300.0},
    }
    assert result["no_activity"] is False
    assert result["edges_capped"] is False
    json.dumps(result)


def test_account_activity_picks_the_account_for_each_side():
    for side, account in (("sender", SENDER), ("receiver", RECEIVER)):
        _, fake = get_activity([], side=side)
        assert any(f"account_key = '{account}'" in call for call in fake.calls)


def test_account_activity_lists_nearest_to_the_alert_first_in_both_directions():
    result, _ = get_activity(
        [
            activity("in", "txn_far", "30|C", -300),
            activity("in", "txn_b", "30|C", 20),
            activity("in", "txn_a", "30|C", -20),
            activity("out", "txn_late", "40|E", 200),
            activity("out", "txn_soon", "40|E", 5),
        ]
    )
    assert [p["txn_id"] for p in result["incoming"]] == ["txn_a", "txn_b", "txn_far"]
    assert [p["txn_id"] for p in result["outgoing"]] == ["txn_soon", "txn_late"]
    assert result["incoming"][0]["offset_minutes"] == -20
    assert result["incoming"][0]["counterparty"] == "30|C"


def test_account_activity_caps_what_is_shown_but_not_what_is_counted():
    rows = [activity("out", f"txn_o{n:02d}", f"4{n}|X", n) for n in range(1, 6)]
    result, _ = get_activity(rows, max_edges=2)
    assert result["edges_capped"] is True
    assert result["n_outgoing"] == 5 and result["n_outgoing_counterparties"] == 5
    assert [p["txn_id"] for p in result["outgoing"]] == ["txn_o01", "txn_o02"]
    assert "txn_o05" not in result["ids"]
    assert result["per_currency_totals"]["outgoing"] == {"US Dollar": 500.0}


def test_account_activity_ids_cover_what_is_shown():
    result, _ = get_activity(
        [activity("in", "txn_i1", "30|C", -5), activity("out", "txn_o1", "40|E", 5)]
    )
    assert result["ids"] == [ALERT_ID, SENDER, "txn_i1", "txn_o1", "30|C", "40|E"]


def test_account_activity_without_payments_is_flagged():
    result, _ = get_activity([])
    assert result["no_activity"] is True
    assert result["per_currency_totals"] == {"incoming": {}, "outgoing": {}}
    assert result["ids"] == [ALERT_ID, SENDER]


def test_account_activity_excludes_the_alert_and_stays_in_the_window_in_sql():
    _, fake = get_activity([], ts="2022-09-14 10:00:00.000", window_h=6)
    statement = fake.calls[1]
    assert f"transaction_id <> '{ALERT_ID}'" in statement
    assert "TIMESTAMP '2022-09-14 04:00:00'" in statement
    assert "TIMESTAMP '2022-09-14 16:00:00'" in statement


def test_account_activity_cutoff_ends_the_sql_window_without_truncating():
    result, fake = get_activity([], as_of="2022-09-14 12:00:00")
    assert "TIMESTAMP '2022-09-14 12:00:00'" in fake.calls[1]
    assert result["window_truncated"] is False
    assert result["hours_observed_after_alert"] == pytest.approx(2.0, abs=0.1)


@pytest.mark.parametrize(
    ("ts", "history_days"),
    [
        ("2022-09-14 10:00:00.000", 3),  # 11, 12 and 13 Sep.
        ("2022-09-15 10:00:00.000", 4),
        ("2022-09-16 10:00:00.000", 5),
    ],
)
def test_account_activity_history_days_count_whole_days_before_the_alert(ts, history_days):
    result, _ = get_activity([], ts=ts)
    assert result["history_days"] == history_days


def test_account_activity_history_is_daily_per_direction_and_currency():
    result, fake = get_activity(
        [],
        history=[
            history_row("2022-09-11", "in", 2, "300.50"),
            history_row("2022-09-11", "out", 1, "100.25"),
            history_row("2022-09-11", "out", 1, "10.00", currency="Euro"),
            history_row("2022-09-13", "in", 4, "40.00"),
        ],
    )
    assert result["daily_history"] == [
        {
            "date": "2022-09-11",
            "n_incoming": 2,
            "n_outgoing": 2,
            "incoming_totals": {"US Dollar": 300.5},
            "outgoing_totals": {"US Dollar": 100.25, "Euro": 10.0},
        },
        {
            "date": "2022-09-13",
            "n_incoming": 4,
            "n_outgoing": 0,
            "incoming_totals": {"US Dollar": 40.0},
            "outgoing_totals": {},
        },
    ]
    history_sql = fake.calls[2]
    assert "txn_date BETWEEN DATE '2022-09-11' AND DATE '2022-09-13'" in history_sql


def test_account_activity_on_the_first_covered_day_has_no_history_query():
    # Alerts only exist on scored days, so build the alert by hand to reach this guard.
    data, fake = data_for()
    alert = {
        "found": True,
        "txn_id": ALERT_ID,
        "ts": "2022-09-11 10:00:00",
        "sender": SENDER,
        "receiver": RECEIVER,
    }
    result = tools.get_account_activity(data, alert, "sender")
    assert result["history_days"] == 0 and result["daily_history"] == []
    assert not any("n_payments" in call for call in fake.calls)


def test_account_activity_rejects_unknown_side_and_missing_alert():
    data, _ = data_for()
    alert = tools.get_alert(data, ALERT_ID, "2022-09-14")
    with pytest.raises(ValueError):
        tools.get_account_activity(data, alert, "both")
    with pytest.raises(ValueError):
        tools.get_account_activity(data, {"found": False}, "sender")


# --- get_neighbour_scores ----------------------------------------------------------------


def test_neighbour_scores_marks_unscored_validation_and_missing():
    scores = pd.DataFrame(
        [
            {
                "transaction_id": "txn_n1",
                "risk_score": "0.91",
                "risk_rank_global": "900",
                "risk_rank_day": "12",
            },
            {
                "transaction_id": "txn_n2",
                "risk_score": "0.40",
                "risk_rank_global": "90000",
                "risk_rank_day": "8000",
            },
            {
                "transaction_id": "txn_n3",
                "risk_score": "0.70",
                "risk_rank_global": "5000",
                "risk_rank_day": None,
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
    assert by_id["txn_n1"]["in_day_top_k"] is True
    assert "has_reasons" not in by_id["txn_n1"]
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


def test_related_alerts_hide_anything_after_the_cutoff():
    queues = {
        "2022-09-14": pd.DataFrame(
            [
                queue_row("txn_before", RECEIVER, "60|F", "2022-09-14 12:00:00.000", 4),
                queue_row("txn_late", RECEIVER, "60|F", "2022-09-14 23:59:59.000", 5),
            ]
        ),
        "2022-09-15": pd.DataFrame(
            [queue_row("txn_next", RECEIVER, "60|F", "2022-09-15 01:00:00.000", 6)]
        ),
    }
    data, fake = data_for(queues=queues)
    result = tools.get_related_alerts(
        data, [RECEIVER], "2022-09-14 10:00:00", 48, as_of="2022-09-14 23:59:59"
    )
    assert [a["txn_id"] for a in result["alerts"]] == ["txn_before", "txn_late"]
    assert len(fake.calls) == 1  # The next day's queue is never read.


def test_related_alerts_accept_counterparty_accounts_from_the_graph():
    queues = {
        "2022-09-14": pd.DataFrame(
            [
                queue_row("txn_up", "30|C", "70|M", "2022-09-14 07:00:00.000", 6),
                queue_row("txn_down", "71|N", "40|E", "2022-09-14 13:00:00.000", 8),
                queue_row("txn_unrelated", "72|O", "73|P", "2022-09-14 10:00:00.000", 9),
            ]
        )
    }
    data, _ = data_for(queues=queues)
    result = tools.get_related_alerts(
        data,
        [SENDER, RECEIVER, "30|C", "40|E"],
        "2022-09-14 10:00:00",
        6,
        exclude_txn_ids=(ALERT_ID,),
    )
    assert [(a["txn_id"], a["account"], a["role"]) for a in result["alerts"]] == [
        ("txn_up", "30|C", "sender"),
        ("txn_down", "40|E", "receiver"),
    ]
    assert {"30|C", "40|E"} <= set(result["ids"])  # Both can be cited.


def test_related_alerts_report_an_alert_once_per_matching_account():
    queues = {
        "2022-09-14": pd.DataFrame(
            [queue_row("txn_both", "30|C", "40|E", "2022-09-14 11:00:00.000", 6)]
        )
    }
    data, _ = data_for(queues=queues)
    result = tools.get_related_alerts(data, ["30|C", "40|E"], "2022-09-14 10:00:00", 6)
    assert [(a["txn_id"], a["account"]) for a in result["alerts"]] == [
        ("txn_both", "30|C"),
        ("txn_both", "40|E"),
    ]
    assert result["ids"] == ["txn_both", "30|C", "40|E"]


def test_related_alerts_limit_how_many_accounts_one_call_may_check():
    data, fake = data_for()
    accounts = [f"{n}|ACC" for n in range(config.AGENT_MAX_RELATED_ACCOUNTS)]
    tools.get_related_alerts(data, accounts, "2022-09-14 10:00:00", 6)  # At the limit.
    with pytest.raises(ValueError, match="At most"):
        tools.get_related_alerts(
            data, accounts + ["99|ACC"], "2022-09-14 10:00:00", 6
        )
    # Duplicates do not count twice.
    tools.get_related_alerts(data, accounts + accounts, "2022-09-14 10:00:00", 6)


def test_related_alerts_need_at_least_one_valid_account():
    data, _ = data_for()
    with pytest.raises(ValueError, match="at least one"):
        tools.get_related_alerts(data, [], "2022-09-14 10:00:00", 6)
    with pytest.raises(ValueError):
        tools.get_related_alerts(data, ["not an account"], "2022-09-14 10:00:00", 6)


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
        tools.get_account_activity(data, alert, "sender", 48),
        tools.get_account_activity(data, alert, "receiver", 48),
    ]
    text = json.dumps(outputs)
    for column in config.AGENT_FORBIDDEN_COLUMNS:
        assert f'"{column}"' not in text
