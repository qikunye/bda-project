import re
from pathlib import Path

import pytest

from src import config
from src.agent import sql

TXN = "txn_613ac569efe8fc2e024adfbeae71b746"
SENDER = "120075|81294D7A0"
RECEIVER = "15126|8127C1990"
TS = "2022-09-14 10:00:00"


def all_builder_outputs():
    return [
        sql.build_alert_query(TXN, "2022-09-14"),
        sql.build_case_graph_query(SENDER, RECEIVER, "2022-09-14 10:00:00.000", 48),
        sql.build_neighbour_scores_query([TXN], ["2022-09-14"]),
        sql.build_day_queue_query("2022-09-15", 500),
        sql.build_account_activity_query(SENDER, TXN, TS, 48),
        sql.build_account_history_query(SENDER, TS),
    ]


@pytest.mark.parametrize("column", sorted(config.AGENT_FORBIDDEN_COLUMNS))
def test_assert_label_free_rejects_every_forbidden_column(column):
    with pytest.raises(sql.LabelLeakError):
        sql.assert_label_free(f"SELECT transaction_id, {column} FROM t")


def test_assert_label_free_is_case_insensitive():
    with pytest.raises(sql.LabelLeakError):
        sql.assert_label_free("SELECT Evaluation_Is_Laundering FROM t")


def test_builders_read_only_the_agent_tables():
    allowed = {
        config.AGENT_SCORES_TABLE,
        config.AGENT_EDGES_TABLE,
        config.AGENT_QUEUE_TABLE,
    }
    for statement in all_builder_outputs():
        sql.assert_label_free(statement)
        tables = set(re.findall(r"FROM\s+([\w.]+)", statement))
        assert tables and tables <= allowed


def test_agent_source_never_names_a_label_column():
    sources = Path("src/agent").glob("*.py")
    text = "\n".join(path.read_text() for path in sources)
    for column in config.AGENT_FORBIDDEN_COLUMNS - {"data_split"}:
        assert column not in text


def test_alert_query_hits_one_partition_and_one_id():
    statement = sql.build_alert_query(TXN, "2022-09-14")
    assert "txn_date = DATE '2022-09-14'" in statement
    assert f"transaction_id = '{TXN}'" in statement
    assert "amount_paid" in statement
    assert "feature_1" not in statement and "reason_1" not in statement


def test_case_graph_query_follows_the_bq2_window_rule():
    statement = " ".join(sql.build_case_graph_query(SENDER, RECEIVER, TS, 6).split())
    at = "TIMESTAMP '2022-09-14 10:00:00'"
    assert f"account_key = '{SENDER}' AND direction = 'in'" in statement
    assert f"account_key = '{RECEIVER}' AND direction = 'out'" in statement
    assert f"transaction_timestamp <= {at}" in statement
    assert f"transaction_timestamp >= {at}" in statement
    assert f"DATE_DIFF('minute', transaction_timestamp, {at}) <= 360" in statement
    assert f"DATE_DIFF('minute', {at}, transaction_timestamp) <= 360" in statement


def test_case_graph_query_prunes_to_the_window_dates():
    statement = sql.build_case_graph_query(SENDER, RECEIVER, "2022-09-14 02:00:00", 48)
    assert "BETWEEN DATE '2022-09-12' AND DATE '2022-09-14'" in statement
    assert "BETWEEN DATE '2022-09-14' AND DATE '2022-09-16'" in statement


def test_case_graph_query_stops_downstream_at_the_cutoff():
    statement = sql.build_case_graph_query(
        SENDER, RECEIVER, "2022-09-14 10:00:00", 48, as_of="2022-09-14 23:59:59"
    )
    assert "BETWEEN DATE '2022-09-14' AND DATE '2022-09-14'" in statement
    assert statement.count("transaction_timestamp <= TIMESTAMP '2022-09-14 23:59:59'") == 1
    assert "BETWEEN DATE '2022-09-12' AND DATE '2022-09-14'" in statement  # Upstream unchanged.


def test_case_graph_query_rejects_a_bad_cutoff():
    with pytest.raises(ValueError):
        sql.build_case_graph_query(SENDER, RECEIVER, TS, 48, as_of="x' --")


def test_neighbour_query_filters_dates_and_ids():
    statement = sql.build_neighbour_scores_query(
        [TXN, "txn_b", TXN], ["2022-09-15", "2022-09-14", "2022-09-15"]
    )
    assert "IN (DATE '2022-09-14', DATE '2022-09-15')" in statement
    assert f"IN ('{TXN}', 'txn_b')" in statement
    assert "has_reasons" not in statement and "feature_1" not in statement


def test_day_queue_query_limits_to_top_k():
    statement = sql.build_day_queue_query("2022-09-15", 100)
    assert "txn_date = DATE '2022-09-15'" in statement
    assert "risk_rank_day <= 100" in statement


@pytest.mark.parametrize(
    "call",
    [
        lambda: sql.build_alert_query("txn_x' OR '1'='1", "2022-09-14"),
        lambda: sql.build_alert_query(TXN, "2022-09-14'"),
        lambda: sql.build_alert_query(TXN, "14 Sep"),
        lambda: sql.build_case_graph_query("x' --", RECEIVER, TS, 48),
        lambda: sql.build_case_graph_query(SENDER, RECEIVER, "yesterday", 48),
        lambda: sql.build_case_graph_query(SENDER, RECEIVER, TS, 5),
        lambda: sql.build_case_graph_query(SENDER, RECEIVER, TS, 73),
        lambda: sql.build_case_graph_query(SENDER, RECEIVER, TS, True),
        lambda: sql.build_neighbour_scores_query([], []),
        lambda: sql.build_day_queue_query("2022-09-15", 0),
        lambda: sql.build_day_queue_query("2022-09-15", config.AGENT_QUEUE_MAX_K + 1),
    ],
)
def test_builders_reject_bad_input(call):
    with pytest.raises(ValueError):
        call()


def test_account_activity_query_covers_both_directions_of_one_account():
    statement = " ".join(sql.build_account_activity_query(SENDER, TXN, TS, 6).split())
    assert f"account_key = '{SENDER}'" in statement
    assert "direction = " not in statement  # Both directions come back, labelled.
    assert f"transaction_id <> '{TXN}'" in statement
    assert "txn_date BETWEEN DATE '2022-09-14' AND DATE '2022-09-14'" in statement
    assert "TIMESTAMP '2022-09-14 04:00:00' AND TIMESTAMP '2022-09-14 16:00:00'" in statement


def test_account_activity_query_never_reaches_past_a_cutoff():
    statement = sql.build_account_activity_query(
        SENDER, TXN, TS, 48, as_of="2022-09-14 23:59:59"
    )
    assert "TIMESTAMP '2022-09-14 23:59:59'" in statement
    assert "2022-09-16" not in statement


def test_account_activity_query_rejects_bad_input():
    with pytest.raises(ValueError):
        sql.build_account_activity_query("x'; DROP TABLE t; --", TXN, TS, 48)
    with pytest.raises(ValueError):
        sql.build_account_activity_query(SENDER, "txn_1' OR '1'='1", TS, 48)
    with pytest.raises(ValueError):
        sql.build_account_activity_query(SENDER, TXN, TS, 500)


def test_account_history_query_stops_before_the_alert_day():
    statement = " ".join(sql.build_account_history_query(SENDER, TS).split())
    assert "txn_date BETWEEN DATE '2022-09-11' AND DATE '2022-09-13'" in statement
    assert "GROUP BY txn_date, direction, payment_currency" in statement
