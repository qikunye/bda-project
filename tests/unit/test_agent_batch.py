import copy
import json

import pandas as pd
import pytest

from src.agent import tools
from src.agent.batch import BatchIncomplete, publish, run_day
from src.agent.loop import LoopSettings
from src.agent.sql import LabelLeakError
from tests.unit.agent_loop_fixtures import DAY, stub_tools, turn

SUBMIT = "submit_decision"


def queue_frame(*alerts):
    """alerts are (txn_id, sender, receiver, rank)."""

    return pd.DataFrame(
        [
            {
                "transaction_id": t,
                "source_account_key": s,
                "target_account_key": r,
                "transaction_timestamp": "2022-09-14 10:00:00",
                "risk_rank_day": str(rank),
            }
            for t, s, r, rank in alerts
        ]
    )


# txn_a and txn_b share account 2|B, so they form one case. txn_c is alone.
QUEUE = (
    ("txn_a", "1|A", "2|B", 1),
    ("txn_b", "3|C", "2|B", 2),
    ("txn_c", "5|E", "6|F", 3),
)


class PerCaseLLM:
    """Looks at the case in the first message, so the order of concurrent runs does not matter."""

    model_id = "per-case-model"

    def converse(self, *, system, messages, tools, force_tool, max_tokens, temperature):
        text = messages[0]["content"][0]["text"]
        rep = text.split("Representative alert: ")[1].split(".")[0]
        if len(messages) == 1:
            return turn(("get_case_graph", {"txn_id": rep}))
        payload = {"verdict": "raise", "confidence": "medium", "claims": []}
        return turn((SUBMIT, payload))


@pytest.fixture
def queue(monkeypatch):
    stub_tools(monkeypatch)
    state = {"frame": queue_frame(*QUEUE)}
    monkeypatch.setattr(
        tools, "load_day_queue", lambda data, day, top_n: state["frame"]
    )
    return state


def run(**kwargs):
    return run_day(None, PerCaseLLM(), DAY, **kwargs)


class FakeDynamo:
    def __init__(self):
        self.decisions: dict[str, dict] = {}
        self.cases: dict[tuple[str, str], dict] = {}

    def put_decisions(self, rows):
        for row in rows:
            self.decisions[row["transaction_id"]] = copy.deepcopy(row)

    def put_cases(self, rows):
        for row in rows:
            self.cases[(row["day"], row["case_id"])] = copy.deepcopy(row)

    def decision_keys_for_day(self, day):
        return {k for k, v in self.decisions.items() if v["day"] == day}

    def case_ids_for_day(self, day):
        return {c for d, c in self.cases if d == day}

    def delete_decisions(self, txn_ids):
        for t in txn_ids:
            del self.decisions[t]

    def delete_cases(self, day, case_ids):
        for c in case_ids:
            del self.cases[(day, c)]


class FakeTraces:
    def __init__(self):
        self.objects: dict[str, dict] = {}

    def put(self, key, body):
        self.objects[key] = json.loads(json.dumps(body, default=str))

    def keys(self, prefix):
        return {k for k in self.objects if k.startswith(prefix)}

    def delete(self, keys):
        for k in keys:
            del self.objects[k]


def stable(rows):
    """Rows without the fields that change on every run."""

    return {
        r["transaction_id"]: {
            k: v
            for k, v in r.items()
            if k not in ("run_id", "created_at", "latency_ms")
        }
        for r in rows
    }


# --- Running a day


def test_a_day_becomes_cases_with_decision_case_and_pointer_rows(queue):
    output = run()

    assert [c.case_id for c, _ in output.runs] == ["case_txn_a", "case_txn_c"]
    assert output.failures == []
    by_id = {r["transaction_id"]: r for r in output.decision_rows}
    assert {k: v["role"] for k, v in by_id.items()} == {
        "txn_a": "representative",
        "txn_b": "member",
        "txn_c": "representative",
    }
    assert by_id["txn_b"]["representative_txn_id"] == "txn_a"
    assert "claims" in by_id["txn_a"] and "claims" not in by_id["txn_b"]
    assert by_id["txn_a"]["trace_key"] == "agent/traces/2022-09-14/txn_a.json"
    assert by_id["txn_a"]["case_id"] == "case_txn_a"
    assert (
        by_id["txn_c"]["case_id"] is None
    )  # A single alert has no case_id on its decision.

    cases = {c["case_id"]: c for c in output.case_rows}
    assert cases["case_txn_a"]["member_txn_ids"] == ["txn_a", "txn_b"]
    assert cases["case_txn_a"]["size"] == 2
    assert cases["case_txn_c"]["size"] == 1  # The cases table lists single alerts too.
    assert cases["case_txn_c"]["verdict"] == "raise"


def test_concurrency_does_not_change_the_result(queue):
    queue["frame"] = queue_frame(
        *[(f"txn_{i}", f"{i}|A", f"{i}|B", i) for i in range(1, 13)]
    )
    serial = run(concurrency=1, run_id="r", created_at="t")
    parallel = run(concurrency=4, run_id="r", created_at="t")
    assert [c.case_id for c, _ in parallel.runs] == [c.case_id for c, _ in serial.runs]
    assert stable(parallel.decision_rows) == stable(serial.decision_rows)
    assert parallel.case_rows == serial.case_rows


def test_run_id_and_time_default_but_can_be_set(queue):
    assert run().run_id.startswith("batch-2022-09-14-")
    output = run(run_id="my-run", created_at="2022-11-08T00:00:00+00:00")
    assert {r["run_id"] for r in output.decision_rows} == {"my-run"}
    assert {r["created_at"] for r in output.case_rows} == {"2022-11-08T00:00:00+00:00"}


def test_a_failing_case_is_recorded_and_the_others_finish(queue, monkeypatch):
    original = tools.get_case_graph

    def broken(data, found, window_h, as_of=None):
        if found["txn_id"] == "txn_c":
            raise RuntimeError("Athena is down")
        return original(data, found, window_h, as_of)

    monkeypatch.setattr(tools, "get_case_graph", broken)
    output = run()
    assert [c.case_id for c, _ in output.runs] == ["case_txn_a"]
    assert output.failures == [
        {"case_id": "case_txn_c", "error": "RuntimeError: Athena is down"}
    ]


def test_a_queue_alert_that_is_missing_is_a_failure(queue, monkeypatch):
    stub_tools(monkeypatch, found=False)
    output = run()
    assert output.runs == [] and len(output.failures) == 2
    assert "not found" in output.failures[0]["error"]


def test_a_label_leak_stops_the_whole_batch(queue, monkeypatch):
    original = tools.get_case_graph

    def leaky(data, found, window_h, as_of=None):
        result = original(data, found, window_h, as_of)
        result["is_laundering"] = True
        return result

    monkeypatch.setattr(tools, "get_case_graph", leaky)
    with pytest.raises(LabelLeakError):
        run()


def test_settings_are_passed_to_every_case(queue):
    # The fake model ignores the forced submit, so a one-step limit sends every case to S1.
    output = run(settings=LoopSettings(max_steps=1))
    assert [r["stats"]["fallback"] for _, r in output.runs] == [True, True]


# --- What a daily run can see


def test_a_day_is_triaged_as_of_the_end_of_that_day(queue, monkeypatch):
    calls = stub_tools(monkeypatch)
    run()
    assert calls.as_of_seen
    assert set(calls.as_of_seen) == {"2022-09-14 23:59:59"}


def test_the_cutoff_applies_even_to_settings_passed_in(queue, monkeypatch):
    calls = stub_tools(monkeypatch)
    run(settings=LoopSettings(max_steps=1))
    assert set(calls.as_of_seen) == {"2022-09-14 23:59:59"}


def test_each_transaction_gets_exactly_one_row(queue):
    rows = run().decision_rows
    ids = [r["transaction_id"] for r in rows]
    assert len(ids) == len(set(ids)) == 3


# --- Saving, and rerunning


def test_publish_saves_decisions_cases_and_traces(queue):
    dynamo, traces = FakeDynamo(), FakeTraces()
    output = run()
    report = publish(output, dynamo, traces)

    assert (report.decisions_written, report.cases_written, report.traces_written) == (
        3,
        2,
        2,
    )
    assert set(dynamo.decisions) == {"txn_a", "txn_b", "txn_c"}
    assert set(dynamo.cases) == {(DAY, "case_txn_a"), (DAY, "case_txn_c")}
    assert set(traces.objects) == {
        "agent/traces/2022-09-14/txn_a.json",
        "agent/traces/2022-09-14/txn_c.json",
    }
    body = traces.objects["agent/traces/2022-09-14/txn_a.json"]
    assert body["run_id"] == output.run_id and body["result"]["status"] == "ok"


def test_a_rerun_leaves_the_same_rows(queue):
    dynamo, traces = FakeDynamo(), FakeTraces()
    publish(run(), dynamo, traces)
    first = (
        stable(dynamo.decisions.values()),
        copy.deepcopy(dynamo.cases),
        set(traces.objects),
    )

    report = publish(run(), dynamo, traces)
    assert (
        report.decisions_deleted == report.cases_deleted == report.traces_deleted == 0
    )
    assert stable(dynamo.decisions.values()) == first[0]
    assert {
        k: {x: y for x, y in v.items() if x not in ("run_id", "created_at")}
        for k, v in dynamo.cases.items()
    } == {
        k: {x: y for x, y in v.items() if x not in ("run_id", "created_at")}
        for k, v in first[1].items()
    }
    assert set(traces.objects) == first[2]


def test_a_rerun_removes_rows_that_the_new_run_did_not_write(queue):
    dynamo, traces = FakeDynamo(), FakeTraces()
    publish(run(), dynamo, traces)

    # txn_b leaves the queue and txn_d joins, so the old case and its rows are stale.
    queue["frame"] = queue_frame(
        ("txn_a", "1|A", "2|B", 1),
        ("txn_c", "5|E", "6|F", 2),
        ("txn_d", "8|G", "9|H", 3),
    )
    report = publish(run(), dynamo, traces)

    assert set(dynamo.decisions) == {"txn_a", "txn_c", "txn_d"}
    assert report.decisions_deleted == 1  # txn_b
    assert dynamo.cases[(DAY, "case_txn_a")]["member_txn_ids"] == ["txn_a"]
    assert (DAY, "case_txn_d") in dynamo.cases
    assert "agent/traces/2022-09-14/txn_d.json" in traces.objects


def test_a_rerun_clears_a_case_that_no_longer_exists(queue):
    dynamo, traces = FakeDynamo(), FakeTraces()
    publish(run(), dynamo, traces)
    queue["frame"] = queue_frame(("txn_a", "1|A", "2|B", 1))
    report = publish(run(), dynamo, traces)
    assert (report.cases_deleted, report.traces_deleted) == (1, 1)
    assert set(dynamo.cases) == {(DAY, "case_txn_a")}
    assert set(traces.objects) == {"agent/traces/2022-09-14/txn_a.json"}


def test_other_days_are_never_touched(queue):
    dynamo, traces = FakeDynamo(), FakeTraces()
    other = {"transaction_id": "txn_old", "day": "2022-09-15", "role": "representative"}
    dynamo.put_decisions([other])
    dynamo.put_cases([{"day": "2022-09-15", "case_id": "case_txn_old"}])
    traces.put("agent/traces/2022-09-15/txn_old.json", {})
    publish(run(), dynamo, traces)
    assert "txn_old" in dynamo.decisions
    assert ("2022-09-15", "case_txn_old") in dynamo.cases
    assert "agent/traces/2022-09-15/txn_old.json" in traces.objects


def test_a_failed_case_means_nothing_is_saved_or_deleted(queue, monkeypatch):
    dynamo, traces = FakeDynamo(), FakeTraces()
    publish(run(), dynamo, traces)
    before = (
        copy.deepcopy(dynamo.decisions),
        copy.deepcopy(dynamo.cases),
        set(traces.objects),
    )

    def broken(data, found, window_h, as_of=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(tools, "get_case_graph", broken)
    with pytest.raises(BatchIncomplete, match="nothing was saved"):
        publish(run(), dynamo, traces)
    assert (dynamo.decisions, dynamo.cases, set(traces.objects)) == before
