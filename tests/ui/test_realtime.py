"""Real-time contract boundaries and manual-refresh UI, with no external services."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from streamlit.testing.v1 import AppTest

from src.ui.components.realtime import timestamp_text
from src.ui.pages.realtime import DEMO_SOURCE, LIVE_SOURCE
from src.ui.services.realtime import (
    ALERTS_TABLE,
    BRIEFS_TABLE,
    MAX_LOOKUPS,
    RealtimeService,
    map_documented_alert,
    map_documented_brief,
)
from src.ui.services.realtime_demo import DEMO_GROUPS, DemoRealtimeService, demo_records

APP = Path(__file__).resolve().parents[2] / "app.py"


class FakeRealtimeDynamo:
    """No scan, query, write, Kinesis, Lambda or SQS interface exists."""

    def __init__(self, items=None, errors=None):
        self.items = items or {}
        self.errors = errors or {}
        self.calls = []

    def Table(self, table):
        parent = self
        class Table:
            def get_item(self, *, Key, ConsistentRead):
                assert set(Key) == {"txn"} and ConsistentRead is True
                parent.calls.append((table, Key["txn"]))
                if table in parent.errors:
                    raise parent.errors[table]
                item = parent.items.get((table, Key["txn"]))
                return {"Item": deepcopy(item)} if item is not None else {}
        return Table()


def text(app):
    return "\n".join(str(element.value) for kind in (
        "text", "caption", "info", "warning", "error", "markdown", "subheader", "code",
    ) for element in app.get(kind))


def button(app, label):
    return next(widget for widget in app.button if widget.label == label)


def monitor(source=DEMO_SOURCE, service=None):
    app = AppTest.from_file(APP, default_timeout=10)
    app.session_state["ui_navigation_page"] = "Real-Time Monitoring"
    app.session_state["ui_rt_source"] = source
    if service is not None:
        app.session_state["ui_rt_services"] = {source: service}
    app.run()
    assert not app.exception
    return app


def load(app, group=None):
    if group is not None:
        app.selectbox(key="ui_rt_demo_group").set_value(group)
    button(app, "Load known alerts").click().run()
    assert not app.exception
    return app


def test_minimal_live_mapping_uses_only_documented_attributes():
    resource = FakeRealtimeDynamo({
        (ALERTS_TABLE, "txn_a"): {"txn": "txn_a", "amount": "12.50", "risk_score": 0.99, "secret": "PRIVATE"},
        (BRIEFS_TABLE, "txn_a"): {"txn": "txn_a", "narrative": "PRIVATE UNVERIFIED TEXT", "findings": []},
    })
    record = RealtimeService(resource).lookup("txn_a")
    assert record.status == "available"
    assert record.alert.amount == "12.50" and record.alert.score is None
    assert record.alert.timestamp is None and record.alert.currency is None
    assert not record.brief.content_mapped and record.brief.narrative is None
    assert "PRIVATE" not in repr(record)
    assert resource.calls == [(ALERTS_TABLE, "txn_a"), (BRIEFS_TABLE, "txn_a")]


@pytest.mark.parametrize(("has_alert", "has_brief", "status"), [
    (True, True, "available"), (True, False, "pending"),
    (False, False, "not_found"), (False, True, "unpaired"),
])
def test_actual_shared_txn_identity_determines_pair_state(has_alert, has_brief, status):
    items = {}
    for present, table in ((has_alert, ALERTS_TABLE), (has_brief, BRIEFS_TABLE)):
        if present:
            items[(table, "txn_a")] = {"txn": "txn_a"}
    record = RealtimeService(FakeRealtimeDynamo(items)).lookup("txn_a")
    assert record.status == status


@pytest.mark.parametrize(("table", "bad_item"), [
    (ALERTS_TABLE, {"txn": "wrong"}), (BRIEFS_TABLE, {"txn": "wrong"}),
    (ALERTS_TABLE, {"txn": "txn_a", "amount": 12.5}),
    (BRIEFS_TABLE, ["not a record"]), (ALERTS_TABLE, {"amount": "12"}),
])
def test_malformed_or_mismatched_records_are_incompatible(table, bad_item):
    record = RealtimeService(FakeRealtimeDynamo({(table, "txn_a"): bad_item})).lookup("txn_a")
    assert record.status == "incompatible"


def test_optional_and_null_documented_fields_are_safe():
    assert map_documented_alert({"txn": "txn_a", "amount": None}, "txn_a").amount is None
    assert map_documented_alert({"txn": "txn_a"}, "txn_a").amount is None
    assert map_documented_brief(None, "txn_a") is None


@pytest.mark.parametrize(("table", "status"), [(ALERTS_TABLE, "alert_error"), (BRIEFS_TABLE, "brief_error")])
def test_read_errors_do_not_become_missing_records(table, status):
    resource = FakeRealtimeDynamo({(ALERTS_TABLE, "txn_a"): {"txn": "txn_a", "amount": "12"}},
                                  {table: NoCredentialsError()})
    record = RealtimeService(resource).lookup("txn_a")
    assert record.status == status
    failure = record.alert_failure if table == ALERTS_TABLE else record.brief_failure
    assert failure.category == "Authentication failure"
    if table == BRIEFS_TABLE:
        assert record.alert.amount == "12"


def test_unverified_sort_key_requirement_is_reported_as_schema_incompatible():
    error = ClientError({"Error": {"Code": "ValidationException", "Message": "PRIVATE key details"}}, "GetItem")
    record = RealtimeService(FakeRealtimeDynamo(errors={ALERTS_TABLE: error})).lookup("txn_a")
    assert record.status == "incompatible" and "PRIVATE" not in record.alert_failure.message


def test_known_id_groups_are_bounded_deduplicated_and_not_chronologically_listed():
    resource = FakeRealtimeDynamo()
    service = RealtimeService(resource)
    assert [record.txn for record in service.lookup_many(["txn_b", "txn_a", "txn_b"])] == ["txn_b", "txn_a"]
    assert len(resource.calls) == 4
    assert not hasattr(service, "list_recent")
    before = list(resource.calls)
    with pytest.raises(ValueError):
        service.lookup_many([f"txn_{i}" for i in range(MAX_LOOKUPS + 1)])
    assert resource.calls == before


@pytest.mark.parametrize("ids", [[], [""], ["txn\ninvalid"], ["x" * 257]])
def test_invalid_lookup_is_rejected_before_client_creation(ids):
    with pytest.raises(ValueError):
        RealtimeService().lookup_many(ids)


@pytest.mark.parametrize("group", DEMO_GROUPS)
def test_all_demo_groups_render_with_explicit_sample_provenance(group):
    app = load(monitor(), group)
    assert "DEMO DATA" in text(app) and "invented samples" in text(app)
    assert "not event freshness or processing latency" in text(app)
    assert app.dataframe[0].value["Transaction ID"].tolist() == DEMO_GROUPS[group]
    assert "not a chronological recent-alert feed" in text(app)
    assert "pipeline health" in text(app)
    assert "Verdict" not in text(app) and "confidence" not in text(app)


def test_demo_initial_load_and_selection_are_passive_until_manual_actions():
    backend = Mock(wraps=DemoRealtimeService())
    app = monitor(service=backend)
    backend.lookup_many.assert_not_called()
    assert not app.dataframe and button(app, "Refresh loaded alerts").disabled
    load(app)
    backend.lookup_many.assert_called_once()
    app.selectbox(key="ui_rt_selected_txn").set_value("demo_rt_pending").run()
    assert "no brief was stored in this lookup" in text(app)
    app.run()
    backend.lookup_many.assert_called_once()
    app.radio(key="ui_navigation_page").set_value("Alert Queue").run()
    app.radio(key="ui_navigation_page").set_value("Real-Time Monitoring").run()
    assert app.selectbox(key="ui_rt_selected_txn").value == "demo_rt_pending"
    backend.lookup_many.assert_called_once()
    button(app, "Refresh loaded alerts").click().run()
    assert backend.lookup_many.call_count == 2


def test_live_selection_is_passive_and_reads_use_only_the_explicit_id_set():
    resource = FakeRealtimeDynamo({(ALERTS_TABLE, "txn_known"): {"txn": "txn_known", "amount": "22.00"},
                                   (BRIEFS_TABLE, "txn_known"): {"txn": "txn_known", "text": "PRIVATE"}})
    app = monitor(LIVE_SOURCE, RealtimeService(resource))
    assert not resource.calls and not app.dataframe
    assert not app.selectbox
    app.text_area(key="ui_rt_live_ids").set_value("txn_known, txn_missing")
    load(app)
    assert len(resource.calls) == 4
    assert "field names are unverified" in text(app) and "PRIVATE" not in text(app)
    assert "Completed explanation" not in text(app)
    app.selectbox(key="ui_rt_selected_txn").set_value("txn_missing").run()
    assert "not a permanent missing-record conclusion" in text(app)
    app.run()
    assert len(resource.calls) == 4


def test_live_error_is_not_substituted_with_demo_records():
    resource = FakeRealtimeDynamo(errors={ALERTS_TABLE: NoCredentialsError(), BRIEFS_TABLE: NoCredentialsError()})
    app = monitor(LIVE_SOURCE, RealtimeService(resource))
    app.text_area(key="ui_rt_live_ids").set_value("txn_known")
    load(app)
    assert "Authentication failure" in text(app)
    assert "demo_rt_complete" not in text(app)
    assert app.radio(key="ui_rt_source").value == LIVE_SOURCE


def test_live_blank_lookup_does_not_create_clients():
    app = monitor(LIVE_SOURCE)
    load(app)
    assert app.error and not app.dataframe


def test_refresh_can_observe_a_later_brief_without_claiming_pipeline_latency():
    resource = FakeRealtimeDynamo({(ALERTS_TABLE, "txn_known"): {"txn": "txn_known"}})
    app = monitor(LIVE_SOURCE, RealtimeService(resource))
    app.text_area(key="ui_rt_live_ids").set_value("txn_known")
    load(app)
    assert "no brief was stored in this lookup" in text(app)
    resource.items[(BRIEFS_TABLE, "txn_known")] = {"txn": "txn_known"}
    app.run()
    assert "no brief was stored in this lookup" in text(app)
    assert len(resource.calls) == 2
    button(app, "Refresh loaded alerts").click().run()
    assert "Brief record available" in text(app) and len(resource.calls) == 4


def test_source_switch_discards_snapshot_and_does_not_execute_live_reads():
    app = load(monitor())
    app.radio(key="ui_rt_source").set_value(LIVE_SOURCE).run()
    assert not app.dataframe and "ui_rt_snapshot" not in app.session_state
    assert "demo_rt_complete" not in text(app)


def test_demo_explanation_is_not_a_batch_agent_finding_and_optional_values_are_not_zero():
    app = load(monitor(), "Completed explanation")
    assert "Simulated explanation:" in text(app)
    assert "not independently validated investigation-agent findings" in text(app)
    assert "Illustrative pass-through hypothesis" in text(app)
    assert "Validated findings" not in [heading.value for heading in app.subheader]
    load(app, "Missing optional fields")
    assert all(metric.value == "Unavailable / not mapped" for metric in app.metric)


def test_live_mapping_never_uses_demo_timestamps_or_unknown_model_attributes():
    resource = FakeRealtimeDynamo({(ALERTS_TABLE, "txn_known"): {
        "txn": "txn_known", "timestamp": "2026-11-08T01:00:00Z", "score": 0.99, "rank": 1,
    }})
    record = RealtimeService(resource).lookup("txn_known")
    assert record.alert.timestamp is None and record.alert.score is None
    assert timestamp_text(None) == "Unavailable / not mapped"
    assert timestamp_text("2026-11-08T09:00:00+08:00") == "2026-11-08 01:00:00 UTC"
    assert "timezone unspecified" in timestamp_text("2026-11-08T01:00:00")


def test_demo_data_is_repeatable_and_unpaired_records_do_not_claim_an_arrival_order():
    first, second = demo_records(), demo_records()
    for txn in first:
        assert (first[txn].status, first[txn].alert, first[txn].brief, first[txn].detail) == (
            second[txn].status, second[txn].alert, second[txn].brief, second[txn].detail,
        )
    app = load(monitor(), "Unpaired brief record")
    assert "Arrival order, retention and processing status cannot be inferred" in text(app)
