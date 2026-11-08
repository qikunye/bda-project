import pandas as pd
import pytest

from scripts import rebuild_tables
from scripts.rebuild_tables import (
    compare_fingerprints,
    date_ranges,
    rebuild,
    render_sql,
    statements,
    table_prefix,
)
from src import config
from src.aws.athena import AthenaQueryError


def test_scores_cover_the_scored_days_and_edges_start_three_days_earlier():
    assert date_ranges() == {
        "scores": ("2022-09-14", "2022-09-16"),
        "edges": ("2022-09-11", "2022-09-16"),
    }


def test_adding_a_day_widens_both_ranges(monkeypatch):
    monkeypatch.setattr(
        config,
        "AGENT_SCORED_DATES",
        ("2022-09-14", "2022-09-15", "2022-09-16", "2022-09-18"),
    )
    assert date_ranges() == {
        "scores": ("2022-09-14", "2022-09-18"),
        "edges": ("2022-09-11", "2022-09-18"),
    }


def test_render_replaces_every_range_and_drops_the_semicolon():
    text = "WHERE a BETWEEN DATE '2022-01-01' AND DATE '2022-01-02'\nAND b BETWEEN DATE '2022-01-01' AND DATE '2022-01-02';\n"
    out = render_sql(text, "2022-09-14", "2022-09-18")
    assert out.count("BETWEEN DATE '2022-09-14' AND DATE '2022-09-18'") == 2
    assert "2022-01" not in out and not out.endswith(";")


def test_render_refuses_sql_with_no_range():
    with pytest.raises(ValueError, match="No date range"):
        render_sql("SELECT 1", "a", "b")


def test_the_current_files_render_to_the_dates_they_were_built_with():
    built = dict(statements())
    assert list(built) == ["agent_scores_v2", "agent_edges_v2", "agent_day_queue_v2"]
    assert "BETWEEN DATE '2022-09-14' AND DATE '2022-09-16'" in built["agent_scores_v2"]
    assert "BETWEEN DATE '2022-09-11' AND DATE '2022-09-16'" in built["agent_edges_v2"]
    assert "risk_rank_day <= 5000" in built["agent_day_queue_v2"]
    for sql in built.values():
        assert sql.startswith("CREATE TABLE") and not sql.endswith(";")
        assert "--" not in sql and "DROP" not in sql and "aws s3" not in sql


def test_only_agent_tables_can_be_cleared():
    assert (
        table_prefix("agent_scores_v2")
        == "s3://is459-aml-g1t5/agent/tables/agent_scores_v2/"
    )
    for bad in ("", "bq1_risk_scores_medium_v2", "../silver", "agent_scores_v2/.."):
        with pytest.raises(ValueError):
            table_prefix(bad)


def test_only_days_present_both_times_are_compared():
    before = {"2022-09-14": ("10", "aa"), "2022-09-15": ("5", "bb")}
    after = {
        "2022-09-14": ("10", "aa"),
        "2022-09-15": ("5", "cc"),
        "2022-09-18": ("1", "dd"),
    }
    assert compare_fingerprints(before, after) == [
        "2022-09-15: ('5', 'bb') -> ('5', 'cc')"
    ]
    assert compare_fingerprints({}, after) == []


class Fakes:
    def __init__(self, fingerprints_after=None, fail_first_query=False):
        self.events = []
        self.fingerprints = [
            {"2022-09-14": ("10", "aa")},
            fingerprints_after
            or {"2022-09-14": ("10", "aa"), "2022-09-18": ("1", "zz")},
        ]
        self.fail_first_query = fail_first_query

    def execute(self, sql):
        self.events.append(
            (
                "sql",
                sql.split("\n")[0][:40]
                if "CREATE" not in sql
                else "CREATE " + sql.split("is459_aml_gold.")[1].split("\n")[0],
            )
        )

    def query(self, sql):
        if self.fail_first_query and not self.fingerprints[0] is None:
            self.fail_first_query = False
            raise AthenaQueryError("table not found")
        self.events.append(("fingerprint",))
        data = self.fingerprints.pop(0) if not self.fail_first_query else {}
        return pd.DataFrame(
            [{"txn_date": d, "n": n, "fp": f} for d, (n, f) in data.items()]
        )

    def list_keys(self, prefix):
        self.events.append(("list", prefix))
        return ["k1", "k2"]

    def delete_keys(self, keys):
        self.events.append(("delete", tuple(keys)))


def run_rebuild(fakes):
    return rebuild(
        execute=fakes.execute,
        query=fakes.query,
        list_keys=fakes.list_keys,
        delete_keys=fakes.delete_keys,
        log=lambda *_: None,
    )


def test_tables_are_dropped_last_first_then_built_in_order_and_ranks_are_checked():
    fakes = Fakes()
    assert run_rebuild(fakes) == []
    sql_events = [e[1] for e in fakes.events if e[0] == "sql"]
    assert sql_events[:3] == [
        "DROP TABLE IF EXISTS is459_aml_gold.agent_day_queue_v2"[:40],
        "DROP TABLE IF EXISTS is459_aml_gold.agent_edges_v2"[:40],
        "DROP TABLE IF EXISTS is459_aml_gold.agent_scores_v2"[:40],
    ]
    assert sql_events[3:] == [
        "CREATE agent_scores_v2",
        "CREATE agent_edges_v2",
        "CREATE agent_day_queue_v2",
    ]
    kinds = [e[0] for e in fakes.events]
    assert (
        kinds[0] == "fingerprint" and kinds[-1] == "fingerprint"
    )  # before anything is dropped
    assert [e[1] for e in fakes.events if e[0] == "list"] == [
        "s3://is459-aml-g1t5/agent/tables/agent_day_queue_v2/",
        "s3://is459-aml-g1t5/agent/tables/agent_edges_v2/",
        "s3://is459-aml-g1t5/agent/tables/agent_scores_v2/",
    ]


def test_a_day_whose_ranks_changed_is_reported():
    changed = run_rebuild(Fakes(fingerprints_after={"2022-09-14": ("10", "XX")}))
    assert changed == ["2022-09-14: ('10', 'aa') -> ('10', 'XX')"]


def test_a_bad_sql_file_fails_before_anything_is_dropped(monkeypatch):
    def broken():
        raise ValueError("No date range found in the SQL to replace.")

    monkeypatch.setattr(rebuild_tables, "statements", broken)
    fakes = Fakes()
    with pytest.raises(ValueError):
        run_rebuild(fakes)
    assert fakes.events == []


def test_without_yes_it_touches_nothing(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["rebuild_tables"])
    monkeypatch.setattr(
        rebuild_tables, "create_athena_client", lambda *_: pytest.fail("used AWS")
    )
    monkeypatch.setattr(
        rebuild_tables.boto3, "client", lambda *_, **__: pytest.fail("used AWS")
    )
    assert rebuild_tables.main() == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "agent_scores_v2" in out


def test_a_day_the_tables_already_hold_changes_nothing():
    fakes = Fakes()
    fakes.fingerprints = [
        {"2022-09-14": ("10", "aa"), "2022-09-15": ("5", "bb"), "2022-09-16": ("7", "cc"), "2022-09-17": ("1", "dd")}
    ]
    assert run_rebuild(fakes) == []
    assert [e[0] for e in fakes.events] == ["fingerprint"]


def test_a_rebuild_that_would_drop_a_later_day_is_refused(monkeypatch):
    monkeypatch.setattr(config, "AGENT_SCORED_DATES", ("2022-09-14", "2022-09-15", "2022-09-16", "2022-09-17", "2022-09-18"))
    fakes = Fakes()
    fakes.fingerprints = [{"2022-09-14": ("10", "aa"), "2022-09-19": ("1", "zz")}]
    with pytest.raises(RuntimeError, match="2022-09-19"):
        run_rebuild(fakes)
    assert [e[0] for e in fakes.events] == ["fingerprint"]


def test_last_day_comes_from_the_environment(monkeypatch):
    import importlib

    monkeypatch.setenv("AGENT_LAST_DAY", "2022-09-18")
    fresh = importlib.reload(config)
    try:
        assert fresh.AGENT_SCORED_DATES[-2:] == ("2022-09-17", "2022-09-18")
        assert fresh.AGENT_DATA_HORIZON == "2022-09-18 23:59:59"
        monkeypatch.setenv("AGENT_LAST_DAY", "2022-09-01")
        with pytest.raises(ValueError):
            importlib.reload(config)
    finally:
        monkeypatch.delenv("AGENT_LAST_DAY")
        importlib.reload(config)
