"""Discovery and dashboard regressions; all provider responses are invented."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from boto3.dynamodb.conditions import ConditionExpressionBuilder
from botocore.exceptions import ClientError
from streamlit.testing.v1 import AppTest

from src.ui.components.realtime import (
    explanation_status,
    monitoring_rows,
    threshold_status,
)
from src.ui.pages.realtime import DEMO_SOURCE, INDEX_SOURCE, S3_SOURCE, filter_records
from src.ui.services.realtime import (
    RealtimeRecord,
    RealtimeSchemaError,
    RealtimeService,
)
from src.ui.services.realtime_demo import demo_records
from src.ui.services.realtime_feed import (
    MAX_PAGES,
    RealtimeFeedService,
    explanation_txn,
)
from tests.ui.test_realtime import APP, FakeRealtimeDynamo, button, monitor, text

DAY = date(2022, 9, 16)
PREFIX = "rt/briefs/dt=2022-09-16/"


class Provider:
    """Only paginated discovery is exposed; any write/body/scan fails the test."""

    def __init__(self, operation, pages=(), error=None):
        self.operation, self.pages, self.error = operation, pages, error
        self.calls, self.yielded = [], 0
        self.meta = SimpleNamespace(client=self)

    def get_paginator(self, operation):
        assert operation == self.operation
        return self

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        for page in self.pages:
            self.yielded += 1
            yield page


def lookup():
    return Mock(lookup_many=lambda ids: [RealtimeRecord(txn, "pending") for txn in ids])


def entry(txn, ms=100):
    return {"txn": txn, "feed_day": DAY.isoformat(), "scored_at_ms": ms}


def test_index_query_contract_reverse_order_and_full_cursor_pagination():
    provider = Provider("query", [
        {"Items": [entry("txn_a", 200)], "LastEvaluatedKey": {"txn": "txn_a", "feed_day": DAY.isoformat(), "scored_at_ms": 200}},
        {"Items": [entry("txn_c"), entry("txn_b")]},
    ])
    result = RealtimeFeedService(index_name="by-processing-day", dynamo=provider, lookup=lookup()).recent(DAY, 3)
    assert [r.txn for r in result.records] == ["txn_a", "txn_b", "txn_c"]
    assert not result.limited and provider.yielded == 2
    call = provider.calls[0]
    assert call["TableName"] == "is459_rt_alerts" and call["IndexName"] == "by-processing-day"
    assert call["ScanIndexForward"] is False and call["ConsistentRead"] is False
    assert call["PaginationConfig"] == {"PageSize": 3, "MaxItems": 9}
    built = ConditionExpressionBuilder().build_expression(call["KeyConditionExpression"], is_key_condition=True)
    assert list(built.attribute_name_placeholders.values()) == ["feed_day"]
    assert list(built.attribute_value_placeholders.values()) == ["2022-09-16"]


def test_index_unconfigured_creates_no_clients():
    with pytest.raises(ValueError, match="not configured"):
        RealtimeFeedService().recent(DAY)


@pytest.mark.parametrize("item", [entry("txn_a", None), entry("txn_a", True),
                                   {**entry("txn_a"), "feed_day": "2022-09-15"}, entry(None)])
def test_index_rejects_bad_keys_and_processing_types(item):
    with pytest.raises(RealtimeSchemaError):
        RealtimeFeedService(index_name="index", dynamo=Provider("query", [{"Items": [item]}]), lookup=lookup()).recent(DAY)


@pytest.mark.parametrize("mode", ["recent", "completed"])
def test_both_discovery_modes_stop_at_three_pages(mode):
    if mode == "recent":
        provider = Provider("query", [{"Items": [], "LastEvaluatedKey": {"txn": "txn_a"}}] * 10)
        service = RealtimeFeedService(index_name="index", dynamo=provider, lookup=lookup())
    else:
        provider = Provider("list_objects_v2", [{"Contents": [], "IsTruncated": True}] * 10)
        service = RealtimeFeedService(s3=provider, lookup=lookup())
    assert getattr(service, mode)(DAY).limited
    assert provider.yielded == MAX_PAGES


@pytest.mark.parametrize("key,expected", [
    (PREFIX + "txn_abc123.json", "txn_abc123"), (PREFIX + "txn_.json", None),
    (PREFIX + "txn_bad-id.json", None), (PREFIX + "nested/txn_abc.json", None),
    (PREFIX + "txn_abc.json.bak", None), ("rt/briefs/dt=2022-09-15/txn_a.json", None),
    (PREFIX + "txn_" + "a" * 253 + ".json", None), (None, None),
])
def test_filename_contract(key, expected):
    assert explanation_txn(key, PREFIX) == expected


def test_s3_prefix_dedup_skip_and_bounded_chunked_lookups():
    provider = Provider("list_objects_v2", [
        {"Contents": [{"Key": PREFIX + "txn_a.json"}, {"Key": PREFIX + "txn_a.json"}, {"Key": PREFIX + "wrong"}], "IsTruncated": True},
        {"Contents": [{"Key": PREFIX + f"txn_{i}.json"} for i in range(49)]},
    ])
    keyed = Mock(side_effect=lambda ids: [RealtimeRecord(txn, "pending") for txn in ids])
    result = RealtimeFeedService(s3=provider, lookup=SimpleNamespace(lookup_many=keyed)).completed(DAY, 50)
    assert len(result.records) == len({r.txn for r in result.records}) == 50
    assert result.skipped == 1 and not result.limited
    assert [len(c.args[0]) for c in keyed.call_args_list] == [20, 20, 10]
    assert provider.calls == [{"Bucket": "is459-aml-g1t5", "Prefix": PREFIX,
                               "PaginationConfig": {"PageSize": 50, "MaxItems": 150}}]


def test_s3_limit_and_empty_prefix_do_not_load_records():
    keyed = Mock()
    service = RealtimeFeedService(s3=Provider("list_objects_v2", [{}]), lookup=keyed)
    assert service.completed(DAY).records == []
    keyed.lookup_many.assert_not_called()
    service = RealtimeFeedService(s3=Provider("list_objects_v2", [{"Contents": [
        {"Key": PREFIX + "txn_a.json"}], "IsTruncated": True}]), lookup=lookup())
    assert service.completed(DAY, 1).limited


@pytest.mark.parametrize("limit", [0, 51, True, 1.5])
def test_limits_validated_before_creating_clients(limit):
    with pytest.raises(ValueError):
        RealtimeFeedService().completed(DAY, limit)


def test_risk_filters_missing_scores_and_thresholds():
    records = list(demo_records().values())
    assert [r.txn for r in filter_records(records, "Meets scoring threshold")] == ["demo_rt_complete"]
    assert [r.txn for r in filter_records(records, "Below scoring threshold")] == ["demo_rt_pending"]
    assert [r.txn for r in filter_records(records, "Minimum score", .5)] == ["demo_rt_complete"]
    assert len(filter_records(records)) == len(records)
    assert filter_records(records, status="Fallback / needs review")[0].txn == "demo_rt_fallback"
    assert threshold_status(demo_records()["demo_rt_optional"]) == "Threshold unavailable"
    assert explanation_status(demo_records()["demo_rt_alert_error"]) == "AWS read failure"
    row = monitoring_rows([demo_records()["demo_rt_complete"]])[0]
    assert {"Transaction timestamp", "Processing timestamp", "Sender", "Receiver", "Threshold status"} <= row.keys()


def test_demo_dashboard_is_explicit_and_selection_refresh_are_local():
    app = monitor()
    assert not app.dataframe
    app.button(key="ui_rt_feed_load").click().run()
    assert not app.exception and len(app.dataframe[0].value) == 10
    app.selectbox(key="ui_rt_feed_selected").set_value("demo_rt_pending").run()
    button(app, "Refresh").click().run()
    assert app.selectbox(key="ui_rt_feed_selected").value == "demo_rt_pending"
    app.selectbox(key="ui_rt_feed_status").set_value("Fallback / needs review").run()
    assert app.selectbox(key="ui_rt_feed_selected").value == "demo_rt_fallback"
    assert "template fallback" in text(app)


def test_live_discovery_refresh_filter_selection_source_date_isolation():
    provider = Provider("list_objects_v2", [{"Contents": [{"Key": PREFIX + "txn_a.json"}, {"Key": PREFIX + "txn_b.json"}]}])
    resource = FakeRealtimeDynamo()
    service = RealtimeFeedService(s3=provider, lookup=RealtimeService(resource))
    app = monitor()
    app.session_state["ui_rt_feed_services"] = {(S3_SOURCE, ""): service}
    app.radio(key="ui_rt_feed_source").set_value(S3_SOURCE).run()
    assert provider.calls == []
    app.button(key="ui_rt_feed_load").click().run()
    assert not app.exception and len(resource.calls) == 4
    app.selectbox(key="ui_rt_feed_selected").set_value("txn_b").run()
    app.run()
    assert len(provider.calls) == 1 and len(resource.calls) == 4
    app.radio(key="ui_navigation_page").set_value("Alert Queue").run()
    app.radio(key="ui_navigation_page").set_value("Real-Time Monitoring").run()
    assert app.radio(key="ui_rt_feed_source").value == S3_SOURCE
    assert app.selectbox(key="ui_rt_feed_selected").value == "txn_b"
    assert len(provider.calls) == 1 and len(resource.calls) == 4
    button(app, "Refresh").click().run()
    assert len(provider.calls) == 2 and len(resource.calls) == 8
    assert app.selectbox(key="ui_rt_feed_selected").value == "txn_b"
    app.date_input(key="ui_rt_day_replay").set_value(date(2022, 9, 15)).run()
    assert not app.dataframe and len(provider.calls) == 2
    app.radio(key="ui_rt_feed_source").set_value(DEMO_SOURCE).run()
    assert not app.dataframe and len(provider.calls) == 2


@pytest.mark.parametrize("mode,code", [(S3_SOURCE, "AccessDenied"), (INDEX_SOURCE, "ValidationException"),
                                       (INDEX_SOURCE, "ResourceNotFoundException")])
def test_discovery_failures_are_safe_and_never_substitute_demo(mode, code, monkeypatch):
    monkeypatch.setenv("REALTIME_ALERTS_INDEX", "processing-day")
    provider = Provider("query" if mode == INDEX_SOURCE else "list_objects_v2",
                        error=ClientError({"Error": {"Code": code, "Message": "SECRET"}}, "Discovery"))
    app = monitor()
    app.session_state["ui_rt_feed_services"] = {(mode, "processing-day"): RealtimeFeedService(
        index_name="processing-day", s3=provider, dynamo=provider)}
    app.radio(key="ui_rt_feed_source").set_value(mode).run()
    app.button(key="ui_rt_feed_load").click().run()
    assert not app.exception and not app.dataframe
    assert "No demo records were substituted" in text(app) and "SECRET" not in text(app)
    app.run()
    assert len(provider.calls) == 1


def test_index_option_only_when_configured(monkeypatch):
    monkeypatch.delenv("REALTIME_ALERTS_INDEX", raising=False)
    assert INDEX_SOURCE not in monitor().radio(key="ui_rt_feed_source").options
    monkeypatch.setenv("REALTIME_ALERTS_INDEX", "by-day")
    assert INDEX_SOURCE in monitor().radio(key="ui_rt_feed_source").options


@pytest.mark.parametrize("mode", ["recent", "completed"])
def test_sdk_paginator_emits_explicit_api_limits_and_preserves_cursor(mode):
    """Exercise real botocore pagination with a fake HTTP method, no AWS client."""
    import botocore.session
    from botocore.paginate import Paginator

    session = botocore.session.get_session()
    service_name = "dynamodb" if mode == "recent" else "s3"
    operation = "Query" if mode == "recent" else "ListObjectsV2"
    cursor = {"txn": "txn_a", "feed_day": DAY.isoformat(), "scored_at_ms": 200}
    if mode == "recent":
        responses = iter([{"Items": [entry("txn_a", 200)], "LastEvaluatedKey": cursor},
                          {"Items": [entry("txn_b", 100)]}])
    else:
        responses = iter([{"Contents": [{"Key": PREFIX + "txn_a.json"}], "IsTruncated": True,
                           "NextContinuationToken": "opaque-token"},
                          {"Contents": [{"Key": PREFIX + "txn_b.json"}], "IsTruncated": False}])
    request = Mock(side_effect=lambda **kwargs: next(responses))
    model = session.get_service_model(service_name).operation_model(operation)
    pagination = session.get_component("data_loader").load_service_model(service_name, "paginators-1")["pagination"][operation]
    provider = SimpleNamespace(get_paginator=lambda name: Paginator(request, pagination, model))
    provider.meta = SimpleNamespace(client=provider)
    service = RealtimeFeedService(index_name="index", dynamo=provider, s3=provider, lookup=lookup())
    result = getattr(service, mode)(DAY, 2)
    assert [r.txn for r in result.records] == ["txn_a", "txn_b"]
    first, second = [c.kwargs for c in request.call_args_list]
    if mode == "recent":
        assert first["Limit"] == second["Limit"] == 2
        assert first["ScanIndexForward"] is False
        assert second["ExclusiveStartKey"] == cursor
    else:
        assert first["MaxKeys"] == second["MaxKeys"] == 2
        assert second["ContinuationToken"] == "opaque-token"


def test_canonical_record_permission_and_schema_errors_remain_visible():
    from src.ui.services.realtime import ALERTS_TABLE

    for code, status in [("AccessDeniedException", "alert_error"), ("ValidationException", "incompatible")]:
        error = ClientError({"Error": {"Code": code, "Message": "private"}}, "GetItem")
        resource = FakeRealtimeDynamo(errors={ALERTS_TABLE: error})
        provider = Provider("list_objects_v2", [{"Contents": [{"Key": PREFIX + "txn_a.json"}]}])
        record = RealtimeFeedService(s3=provider, lookup=RealtimeService(resource)).completed(DAY).records[0]
        assert record.status == status and record.alert_failure is not None


def test_index_dashboard_orders_and_filters_canonical_qwen_records_locally(monkeypatch):
    from src.ui.services.realtime import ALERTS_TABLE, BRIEFS_TABLE

    monkeypatch.setenv("REALTIME_ALERTS_INDEX", "by-day")
    provider = Provider("query", [{"Items": [entry("txn_a", 200), entry("txn_b", 100)]}])
    resource = FakeRealtimeDynamo({
        (ALERTS_TABLE, "txn_a"): {"txn": "txn_a", "score": .2, "threshold": .5},
        (ALERTS_TABLE, "txn_b"): {"txn": "txn_b", "score": .8, "threshold": .5},
        (BRIEFS_TABLE, "txn_b"): {"txn": "txn_b", "status": "needs_review", "narrative": "Invented fallback."},
    })
    service = RealtimeFeedService(index_name="by-day", dynamo=provider, lookup=RealtimeService(resource))
    app = monitor()
    app.session_state["ui_rt_feed_services"] = {(INDEX_SOURCE, "by-day"): service}
    app.radio(key="ui_rt_feed_source").set_value(INDEX_SOURCE).run()
    app.date_input(key="ui_rt_day_index").set_value(DAY).run()
    assert not provider.calls
    app.button(key="ui_rt_feed_load").click().run()
    assert not app.exception
    assert app.dataframe[0].value["Transaction ID"].tolist() == ["txn_a", "txn_b"]
    app.selectbox(key="ui_rt_feed_risk").set_value("Meets scoring threshold").run()
    assert app.dataframe[0].value["Transaction ID"].tolist() == ["txn_b"]
    assert "Invented fallback." in text(app) and "template fallback" in text(app)
    assert len(provider.calls) == 1 and len(resource.calls) == 4


@pytest.mark.parametrize("day", ["2022-09-16", None])
def test_invalid_date_is_rejected_without_any_clients(day):
    with pytest.raises(ValueError):
        RealtimeFeedService(index_name="by-day").recent(day)
    with pytest.raises(ValueError):
        RealtimeFeedService().completed(day)


def test_saved_replay_labels_default_date_and_empty_guidance():
    provider = Provider("list_objects_v2", [{}])
    app = monitor()
    app.session_state["ui_rt_feed_services"] = {(S3_SOURCE, ""): RealtimeFeedService(s3=provider, lookup=lookup())}
    app.radio(key="ui_rt_feed_source").set_value(S3_SOURCE).run()
    assert app.radio(key="ui_rt_feed_source").label == "Data Source"
    assert S3_SOURCE == "Saved Replay Results"
    assert app.date_input(key="ui_rt_day_replay").label == "Transaction Date"
    assert app.date_input(key="ui_rt_day_replay").value == DAY
    assert app.button(key="ui_rt_feed_load").label == "Load Results"
    assert "Replay Results" in [heading.value for heading in app.subheader]
    assert "These transactions were replayed through our real-time pipeline using historical data." in text(app)
    technical = next(expander for expander in app.expander if expander.label == "Technical Details")
    assert technical.proto.expanded is False
    assert provider.calls == []
    app.button(key="ui_rt_feed_load").click().run()
    assert not app.exception and not app.dataframe
    assert "No saved results found for this date. Try 16 September 2022, which contains our verified demo examples." in text(app)
    app.date_input(key="ui_rt_day_replay").set_value(date(2022, 9, 15)).run()
    assert len(provider.calls) == 1


def test_old_s3_source_bookmark_migrates_without_reads():
    app = AppTest.from_file(APP)
    app.session_state["ui_navigation_page"] = "Real-Time Monitoring"
    app.session_state["ui_rt_feed_source"] = "Available Completed Explanations (S3 discovery)"
    app.run()
    assert not app.exception and not app.dataframe
    assert app.radio(key="ui_rt_feed_source").value == S3_SOURCE
