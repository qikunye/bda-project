"""Read-only persistence and daily queue tests with fake AWS resources."""

import io
import json
from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest
from botocore.exceptions import ClientError

from src import config
from src.agent import tools
from src.agent.data import AgentData
from src.aws.athena import QueryStats
from src.ui.services.demo import DemoBatchService
from src.ui.services.queue import LiveBatchService, enrich_queue
from src.ui.services.saved_results import (
    DecisionBatch,
    SavedResultsReader,
    decision_state,
)

LOAD_DAY_QUEUE = tools.load_day_queue


def denied(operation="BatchGetItem"):
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "test failure"}}, operation)


class FakeDynamo:
    """Only keyed reads exist; scans and writes would raise AttributeError."""

    def __init__(self, rows=None, cases=None, unprocessed=0):
        self.rows = rows or {}
        self.cases = cases or {}
        self.unprocessed = unprocessed
        self.calls = []
        self.case_calls = []

    def batch_get_item(self, *, RequestItems):
        self.calls.append(deepcopy(RequestItems))
        request = RequestItems[config.AGENT_DECISIONS_TABLE]
        assert request["ConsistentRead"] is True
        assert len(request["Keys"]) <= 100
        if self.unprocessed:
            self.unprocessed -= 1
            return {"UnprocessedKeys": RequestItems}
        return {"Responses": {config.AGENT_DECISIONS_TABLE: [
            deepcopy(self.rows[key["transaction_id"]]) for key in reversed(request["Keys"])
            if key["transaction_id"] in self.rows
        ]}}

    def Table(self, name):
        assert name == config.AGENT_CASES_TABLE
        return self

    def get_item(self, *, Key, ConsistentRead):
        self.case_calls.append(Key)
        assert ConsistentRead is True
        item = self.cases.get((Key["day"], Key["case_id"]))
        return {"Item": deepcopy(item)} if item is not None else {}


class FakeS3:
    def __init__(self, envelopes):
        self.envelopes = envelopes
        self.calls = []
        self.body = None

    def get_object(self, *, Bucket, Key):
        self.calls.append((Bucket, Key))
        self.body = io.BytesIO(json.dumps(self.envelopes[Key]).encode())
        return {"Body": self.body}


def reader_fixture():
    demo = DemoBatchService()
    dynamo = FakeDynamo(deepcopy(demo.rows), deepcopy(demo.cases))
    s3 = FakeS3(deepcopy(demo.envelopes))
    reader = SavedResultsReader(dynamo, s3, sleep=lambda _: None)
    return demo, dynamo, s3, reader


def test_decision_reads_chunk_deduplicate_and_handle_unordered_responses():
    ids = [f"txn_{n}" for n in range(205)]
    dynamo = FakeDynamo({txn_id: {"transaction_id": txn_id} for txn_id in ids[:-1]})
    reader = SavedResultsReader(dynamo)
    batch = reader.read_decisions(ids + ids[:10])
    assert len(dynamo.calls) == 3
    assert set(batch.rows) == set(ids[:-1])
    assert not batch.errors
    assert ids[-1] not in batch.rows


def test_unprocessed_keys_are_retried_with_bounded_backoff():
    sleeps = []
    dynamo = FakeDynamo({"txn_a": {"transaction_id": "txn_a"}}, unprocessed=2)
    batch = SavedResultsReader(dynamo, sleep=sleeps.append).read_decisions(["txn_a"])
    assert list(batch.rows) == ["txn_a"]
    assert sleeps == [0.1, 0.2]
    dynamo.unprocessed = 10
    failed = SavedResultsReader(dynamo, sleep=sleeps.append, max_retries=1).read_decisions(["txn_a"])
    assert "txn_a" in failed.errors and not failed.rows


def test_partial_unprocessed_reads_retry_only_remaining_keys():
    class Partial(FakeDynamo):
        def batch_get_item(self, *, RequestItems):
            if not self.calls:
                self.calls.append(RequestItems)
                return {
                    "Responses": {config.AGENT_DECISIONS_TABLE: [{"transaction_id": "txn_a"}]},
                    "UnprocessedKeys": {config.AGENT_DECISIONS_TABLE: {"Keys": [{"transaction_id": "txn_b"}]}},
                }
            assert RequestItems[config.AGENT_DECISIONS_TABLE]["Keys"] == [{"transaction_id": "txn_b"}]
            return super().batch_get_item(RequestItems=RequestItems)
    resource = Partial({"txn_b": {"transaction_id": "txn_b"}})
    batch = SavedResultsReader(resource, sleep=lambda _: None).read_decisions(["txn_a", "txn_b"])
    assert set(batch.rows) == {"txn_a", "txn_b"}


def test_failed_reads_remain_errors_instead_of_missing(monkeypatch):
    dynamo = FakeDynamo()
    monkeypatch.setattr(dynamo, "batch_get_item", lambda **_: (_ for _ in ()).throw(denied()))
    batch = SavedResultsReader(dynamo).read_decisions(["txn_a"])
    assert decision_state("txn_a", "2022-09-14", batch)[0] == "error"
    assert decision_state("txn_missing", "2022-09-14", batch)[0] == "missing"


def test_representative_outside_slice_is_fetched_then_case_and_stored_trace_are_read():
    demo, dynamo, s3, reader = reader_fixture()
    # Ensure the stored key, not the historical naming convention, controls the read.
    rep = dynamo.rows["txn_samplefan"]
    original = rep["trace_key"]
    rep["trace_key"] = "agent/traces/custom-saved-key.json"
    dynamo.cases[("2022-09-14", "case_txn_samplefan")]["trace_key"] = rep["trace_key"]
    s3.envelopes[rep["trace_key"]] = s3.envelopes.pop(original)
    batch = reader.resolve_decisions(["txn_samplefanmember"])
    assert len(dynamo.calls) == 2
    assert set(batch.rows) == {"txn_samplefan", "txn_samplefanmember"}
    saved = reader.open("txn_samplefanmember", "2022-09-14", batch)
    assert saved.status == "saved" and saved.result["txn_id"] == "txn_samplefan"
    assert dynamo.case_calls == [{"day": "2022-09-14", "case_id": "case_txn_samplefan"}]
    assert s3.calls == [(config.AGENT_TRACE_BUCKET, rep["trace_key"])]
    assert s3.body.closed
    assert demo.results["fan_out_case"]["members"][1]["alert"]["txn_id"] == "txn_samplefanmember"


@pytest.mark.parametrize("location", ["selected", "representative", "case", "envelope"])
def test_mixed_publication_ids_are_rejected(location):
    _, dynamo, s3, reader = reader_fixture()
    locations = {
        "selected": dynamo.rows["txn_samplefanmember"], "representative": dynamo.rows["txn_samplefan"],
        "case": dynamo.cases[("2022-09-14", "case_txn_samplefan")],
        "envelope": s3.envelopes[dynamo.rows["txn_samplefan"]["trace_key"]],
    }
    locations[location]["run_id"] = "different-run"
    saved = reader.open("txn_samplefanmember", "2022-09-14", reader.resolve_decisions(["txn_samplefanmember"]))
    assert saved.status == "incompatible" and saved.result is None


@pytest.mark.parametrize("corruption", ["old_verdict", "missing_field", "bad_member", "bad_metrics", "label"])
def test_old_or_malformed_saved_results_are_rejected(corruption):
    _, dynamo, s3, reader = reader_fixture()
    result = s3.envelopes[dynamo.rows["txn_samplefan"]["trace_key"]]["result"]
    if corruption == "old_verdict":
        result["verdict"] = "raise"
    elif corruption == "missing_field":
        del result["members"][0]["graph"]["upstream"]
    elif corruption == "bad_member":
        result["members"][1]["alert"]["txn_id"] = "txn_wrong"
    elif corruption == "bad_metrics":
        result["money_flow"][0]["per_currency"]["US Dollar"]["upstream_total"] = "not a number"
    else:
        result["trace"][0]["result"]["evaluation_is_laundering"] = 1
    saved = reader.open("txn_samplefan", "2022-09-14", reader.resolve_decisions(["txn_samplefan"]))
    assert saved.status == "incompatible" and saved.result is None


def test_missing_cases_and_failed_trace_reads_are_not_empty_findings(monkeypatch):
    _, dynamo, _, reader = reader_fixture()
    batch = reader.resolve_decisions(["txn_samplefan"])
    dynamo.cases.clear()
    assert reader.open("txn_samplefan", "2022-09-14", batch).status == "error"
    _, _, _, reader = reader_fixture()
    monkeypatch.setattr(reader, "read_trace", lambda _: (_ for _ in ()).throw(denied("GetObject")))
    assert reader.open("txn_samplefan", "2022-09-14", batch).status == "error"


def test_old_decisions_and_missing_representatives_are_distinct():
    demo = DemoBatchService()
    batch = demo.resolve_decisions(["txn_samplefanmember"])
    batch.rows["txn_samplefan"]["verdict"] = "old"
    assert decision_state("txn_samplefanmember", "2022-09-14", batch)[0] == "incompatible"
    batch.rows.pop("txn_samplefan")
    assert decision_state("txn_samplefanmember", "2022-09-14", batch)[0] == "error"


def test_demo_queue_retains_bq1_order_missing_empty_and_fallback():
    demo = DemoBatchService()
    snapshot = demo.load_queue("2022-09-14", 500)
    entries = snapshot.entries
    assert [entry["risk_rank_day"] for entry in entries] == sorted(entry["risk_rank_day"] for entry in entries)
    by_id = {entry["txn_id"]: entry for entry in entries}
    assert by_id["txn_demounprocessed"]["status"] == "missing"
    assert by_id["txn_samplenograph"]["status"] == "empty"
    assert by_id["txn_samplefallback"]["status"] == "fallback"
    assert by_id["txn_samplefanmember"]["representative_txn_id"] == "txn_samplefan"
    assert demo.load_queue("2022-09-15", 500).entries == []
    assert len(demo.load_queue("2022-09-14", 1).entries) == 1
    late = demo.load_queue("2022-09-16", 500)
    assert demo.open(late, "txn_samplequiet").status == "empty"


def test_live_service_uses_existing_queue_sql_with_fake_athena(monkeypatch):
    demo, _, _, saved = reader_fixture()
    snapshot = demo.load_queue("2022-09-14", 500)
    frame = pd.DataFrame(snapshot.entries)
    calls = []
    def run(sql):
        calls.append(sql)
        return frame, QueryStats("fake-query", 123, 1)
    data = AgentData(run)
    monkeypatch.setattr(tools, "load_day_queue", LOAD_DAY_QUEUE)
    live = LiveBatchService(data, saved)
    loaded = live.load_queue("2022-09-14", 500)
    assert "agent_day_queue_v2" in calls[0] and "ORDER BY risk_rank_day" in calls[0]
    assert len(loaded.entries) == len(snapshot.entries)
    assert live.open(loaded, "txn_samplepass").result["txn_id"] == "txn_samplepass"
    live.load_queue("2022-09-14", 500)
    assert len(calls) == 1


def test_decimal_metadata_normalises_and_queue_errors_do_not_drop_rows():
    demo = DemoBatchService()
    snapshot = demo.load_queue("2022-09-14", 500)
    batch = snapshot.decisions
    batch.errors["txn_samplepass"] = "unavailable"
    frame = pd.DataFrame(snapshot.entries).iloc[::-1]
    result = enrich_queue(frame, batch, snapshot.day)
    assert len(result) == len(frame)
    assert next(row for row in result if row["txn_id"] == "txn_samplepass")["status"] == "error"
    resource = FakeDynamo({"txn_a": {"transaction_id": "txn_a", "steps": Decimal(2)}})
    assert SavedResultsReader(resource).read_decisions(["txn_a"]).rows["txn_a"]["steps"] == 2


@pytest.mark.parametrize("column", ["evaluation_is_laundering", "EVALUATION_IS_LAUNDERING"])
def test_labels_in_queue_are_rejected(column):
    demo = DemoBatchService()
    snapshot = demo.load_queue("2022-09-14", 500)
    frame = pd.DataFrame(snapshot.entries)
    frame[column] = 1
    with pytest.raises(ValueError):
        enrich_queue(frame, DecisionBatch(), snapshot.day)


@pytest.mark.parametrize(("day", "top_k"), [("2022-09-17", 500), ("2022-09-14", 0), ("2022-09-14", 5001)])
def test_invalid_live_selection_is_rejected_before_client_creation(day, top_k):
    with pytest.raises(ValueError):
        LiveBatchService().load_queue(day, top_k)


def test_empty_live_queue_needs_no_dynamodb_reads(monkeypatch):
    monkeypatch.setattr(tools, "load_day_queue", lambda *_: pd.DataFrame())
    snapshot = LiveBatchService(data=object()).load_queue("2022-09-14", 500)
    assert snapshot.entries == [] and not snapshot.decisions.rows and not snapshot.decisions.errors
