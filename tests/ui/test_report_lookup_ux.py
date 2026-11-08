"""Report availability and input guidance stay independent of AWS execution."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from streamlit.testing.v1 import AppTest

from src.agent.sample_results import sample_investigations
from src.ui.services.realtime import ALERTS_TABLE, RealtimeService
from src.ui.services.realtime_demo import DemoRealtimeService
from tests.ui.test_batch_dashboard import APP, content, dashboard, open_alert
from tests.ui.test_on_demand import approve, button, setup_live, view, window
from tests.ui.test_realtime import LIVE_SOURCE, FakeRealtimeDynamo, load, monitor, text


def selectors(app):
    return [widget for widget in app.radio if widget.label == "Investigation report"]


def test_saved_only_report_has_heading_retrieval_description_and_no_radio():
    app, runner, backend = setup_live()
    assert not selectors(app)
    assert "Investigation Report" in [heading.value for heading in app.subheader]
    assert "Previously generated investigation, retrieved from AWS. Opening this report does not rerun the AI agent." in content(app)
    metadata = next(widget for widget in app.expander if widget.label == "Saved investigation details")
    assert not metadata.proto.expanded
    app.run()
    runner.assert_not_called()
    backend.open.assert_called_once()


def test_demo_saved_report_does_not_claim_aws_retrieval():
    app = open_alert(dashboard(), "txn_samplepass")
    assert "loaded from synthetic fixtures" in content(app)
    assert "retrieved from AWS" not in content(app)
    assert not selectors(app)


def test_neither_report_has_help_and_explicit_run_controls():
    app = open_alert(dashboard(), "txn_demounprocessed")
    assert "No investigation report is available" in content(app)
    assert button(app, "Run Agent Investigation")
    assert not selectors(app) and not app.get("iframe")
    assert not app.session_state["ui_on_demand_attempts"]


def test_only_new_report_has_own_heading_and_no_radio():
    result = deepcopy(sample_investigations()["pass_through"])
    for alert in (result["alert"], result["members"][0]["alert"]):
        alert["txn_id"] = "txn_demounprocessed"
    result["txn_id"] = result["members"][0]["graph"]["txn_id"] = "txn_demounprocessed"
    result["money_flow"][0]["txn_id"] = "txn_demounprocessed"
    app, runner, _ = setup_live("txn_demounprocessed", Mock(return_value=result))
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    assert "New Investigation Report" in [heading.value for heading in app.subheader]
    assert not selectors(app) and app.get("iframe")
    app.run()
    runner.assert_called_once()
    assert not app.exception


def test_two_reports_use_new_labels_and_passive_switching_preserves_evidence():
    app, runner, backend = setup_live()
    saved = deepcopy(app.session_state["ui_batch_selection"]["saved"])
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    assert view(app).options == ["Previous Investigation", "New Investigation"]
    assert view(app).value == "New Investigation"
    button(app, "txn_samplepassdown1").click().run()
    assert app.session_state["ui_on_demand_display_evidence"] == "txn_samplepassdown1"
    view(app).set_value("Previous Investigation").run()
    button(app, "txn_samplepassup").click().run()
    assert app.session_state["ui_batch_evidence"] == "txn_samplepassup"
    view(app).set_value("New Investigation").run()
    assert app.session_state["ui_on_demand_display_evidence"] == "txn_samplepassdown1"
    assert app.session_state["ui_batch_selection"]["saved"] == saved
    app.run()
    runner.assert_called_once()
    backend.open.assert_called_once()


def test_report_choice_is_safe_when_available_report_count_changes():
    app, runner, _ = setup_live()
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    assert len(selectors(app)) == 1
    window(app).set_value(24).run()
    assert not selectors(app)
    assert "Investigation Report" in [heading.value for heading in app.subheader]
    window(app).set_value(48).run()
    assert view(app).value in view(app).options
    assert not app.exception
    runner.assert_called_once()


@pytest.mark.parametrize("old_label", ["Saved Batch Brief", "On-Demand Brief", "deleted view"])
def test_stale_single_report_widget_choice_is_migrated_before_render(old_label):
    app = open_alert(dashboard(), "txn_samplepass")
    request_id = button(app, "Run Agent Investigation").key.removeprefix("ui_on_demand_execute_").rsplit("_", 1)[0]
    key = f"ui_on_demand_view_{request_id}"
    app.session_state[key] = old_label
    app.run()
    assert not selectors(app) and not app.exception
    assert app.session_state[key] == "Previous Investigation"
    assert "ui_on_demand_services" not in app.session_state


@pytest.mark.parametrize(("input_text", "expected"), [
    ("", "Enter at least one transaction ID from the real-time pipeline."),
    (" \n, ,", "Enter at least one transaction ID from the real-time pipeline."),
    ("txn has spaces", "without spaces or control characters"),
    ("x" * 257, "at most 256"),
    ("txn_\x00bad", "without spaces or control characters"),
    (",".join(f"txn_{index}" for index in range(21)), "at most 20 transaction IDs"),
])
def test_invalid_live_input_is_local_guidance_without_service_or_timestamp(input_text, expected):
    service = Mock()
    app = monitor(LIVE_SOURCE, service)
    app.text_area(key="ui_rt_live_ids").set_value(input_text)
    load(app)
    assert expected in text(app)
    assert "Request failed" not in text(app) and "Debug reference" not in text(app)
    assert "Last lookup completed" not in text(app)
    assert "ui_rt_snapshot" not in app.session_state
    service.lookup_many.assert_not_called()
    assert not app.exception


def test_invalid_input_does_not_even_create_a_service_or_client():
    app = monitor(LIVE_SOURCE)
    load(app)
    assert "ui_rt_services" not in app.session_state
    assert "Enter at least one transaction ID" in text(app)
    assert "batch alert queue" in text(app)
    assert "retrieves stored alerts and Lambda explanations from DynamoDB" in text(app)


@pytest.mark.parametrize("invalid", ["", "two ids", ",".join(f"txn_{i}" for i in range(21))])
def test_invalid_input_preserves_loaded_snapshot_time_selection_and_refresh(invalid):
    service = Mock(wraps=DemoRealtimeService())
    app = monitor(LIVE_SOURCE, service)
    app.text_area(key="ui_rt_live_ids").set_value("demo_rt_complete, demo_rt_pending")
    load(app)
    app.selectbox(key="ui_rt_selected_txn").set_value("demo_rt_pending").run()
    previous = app.session_state["ui_rt_snapshot"]
    app.text_area(key="ui_rt_live_ids").set_value(invalid)
    load(app)
    assert app.session_state["ui_rt_snapshot"] is previous
    assert app.session_state["ui_rt_snapshot"].retrieved_at == previous.retrieved_at
    assert app.selectbox(key="ui_rt_selected_txn").value == "demo_rt_pending"
    assert "no brief was stored in this lookup" in text(app)
    assert len(app.dataframe[0].value) == 2
    service.lookup_many.assert_called_once()
    button(app, "Refresh loaded alerts").click().run()
    assert service.lookup_many.call_count == 2
    assert service.lookup_many.call_args.args[0] == ["demo_rt_complete", "demo_rt_pending"]
    assert not app.error and not app.exception


@pytest.mark.parametrize(("code", "category"), [
    ("ExpiredToken", "Authentication failure"),
    ("AccessDeniedException", "Insufficient permissions"),
    ("ResourceNotFoundException", "Missing data table or resource"),
    ("ThrottlingException", "Transient service failure"),
    ("ValidationException", "incompatible"),
])
def test_real_keyed_lookup_failures_keep_service_classification(code, category):
    failure = ClientError({"Error": {"Code": code, "Message": "PRIVATE"}}, "GetItem")
    resource = FakeRealtimeDynamo(errors={ALERTS_TABLE: failure})
    app = monitor(LIVE_SOURCE, RealtimeService(resource))
    app.text_area(key="ui_rt_live_ids").set_value("txn_known")
    load(app)
    assert category in text(app)
    assert "PRIVATE" not in text(app)
    assert "Debug reference" in text(app)
    assert len(resource.calls) == 2
    assert not app.exception


def test_raised_service_failure_is_not_reported_as_successful_retrieval():
    failure = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "PRIVATE"}}, "GetItem")
    service = Mock()
    service.lookup_many.side_effect = failure
    app = monitor(LIVE_SOURCE, service)
    app.text_area(key="ui_rt_live_ids").set_value("txn_known")
    load(app)
    assert "Insufficient permissions" in text(app) and "Debug reference" in text(app)
    assert "Last lookup completed" not in text(app)
    assert app.session_state["ui_rt_snapshot"].retrieved_at is None


def test_old_monitoring_navigation_state_migrates_to_lookup():
    app = AppTest.from_file(APP)
    app.session_state["ui_navigation_page"] = "Real-Time Monitoring"
    app.run()
    assert app.radio(key="ui_navigation_page").value == "Real-Time Alert Lookup"
    assert app.title[0].value == "Real-Time Alert Lookup"
    assert not app.exception
