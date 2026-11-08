"""Explicit execution, independent evidence views and live errors, entirely with fakes."""

import builtins
import importlib
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, ReadTimeoutError

from src.agent.sample_results import sample_investigations
from src.aws.athena import AthenaQueryError
from src.ui.pages.batch import DEMO_SOURCE, LIVE_SOURCE
from src.ui.services.demo import DemoBatchService
from src.ui.services.errors import describe_failure
from src.ui.services.on_demand import DemoOnDemandService, NoDemoResult, OnDemandService
from src.ui.services.saved_results import DecisionBatch, SavedResultsReader
from tests.ui.test_batch_dashboard import content, dashboard, open_alert
from tests.ui.test_batch_services import FakeDynamo

APP = Path(__file__).resolve().parents[2] / "app.py"


def button(app, label):
    return next(widget for widget in app.button if widget.label == label)


def view(app):
    return next(widget for widget in app.radio if widget.label == "Evidence view")


def window(app):
    return next(widget for widget in app.slider if widget.label == "On-demand window (hours)")


def setup_live(txn_id="txn_samplepass", runner=None):
    backend = DemoBatchService()
    backend.load_queue = Mock(wraps=backend.load_queue)
    backend.open = Mock(wraps=backend.open)
    app = dashboard(backend, LIVE_SOURCE)
    runner = runner if runner is not None else Mock(return_value=sample_investigations()["pass_through"])
    app.session_state["ui_on_demand_services"] = {LIVE_SOURCE: OnDemandService(runner)}
    button(app, "Load / refresh queue").click().run()
    open_alert(app, txn_id)
    return app, runner, backend


def approve(app):
    app.checkbox[0].check().run()
    return app


def test_wrapper_is_lazy_validates_and_returns_runner_result_unchanged():
    result = sample_investigations()["pass_through"]
    runner = Mock(return_value=result)
    service = OnDemandService(runner)
    runner.assert_not_called()
    assert service.run("txn_samplepass", "2022-09-14", 48) is result
    runner.assert_called_once_with("txn_samplepass", "2022-09-14", window_h=48)


@pytest.mark.parametrize(("txn_id", "day", "hours"), [
    ("invalid", "2022-09-14", 48), ("txn_a", "bad-date", 48),
    ("txn_a", "2022-09-14", 5), ("txn_a", "2022-09-14", 73),
])
def test_invalid_requests_never_reach_runner(txn_id, day, hours):
    runner = Mock()
    with pytest.raises(ValueError):
        OnDemandService(runner).run(txn_id, day, hours)
    runner.assert_not_called()


def test_default_wrapper_imports_entry_point_only_on_explicit_run(monkeypatch):
    module = importlib.import_module("src.agent.investigate")
    runner = Mock(return_value=sample_investigations()["pass_through"])
    monkeypatch.setattr(module, "investigate", runner)
    service = OnDemandService()
    runner.assert_not_called()
    service.run("txn_samplepass", "2022-09-14", 48)
    runner.assert_called_once()


def test_demo_execution_does_not_import_real_agent(monkeypatch):
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        assert name != "src.agent.investigate"
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = DemoOnDemandService().run("txn_samplepass", "2022-09-14", 48)
    assert result == sample_investigations()["pass_through"]


@pytest.mark.parametrize(("txn_id", "day", "hours"), [
    ("txn_demounprocessed", "2022-09-14", 48), ("txn_samplepass", "2022-09-14", 24),
    ("txn_samplefan", "2022-09-14", 48), ("txn_samplefanmember", "2022-09-14", 48),
    ("txn_samplequiet", "2022-09-16", 48),
])
def test_demo_never_manufactures_or_relabels_a_fixture(txn_id, day, hours):
    with pytest.raises(NoDemoResult):
        DemoOnDemandService().run(txn_id, day, hours)


def test_live_acknowledgement_and_separate_click_are_both_required():
    app, runner, backend = setup_live()
    assert button(app, "Run Agent Investigation").disabled
    runner.assert_not_called()
    approve(app)
    runner.assert_not_called()
    assert not button(app, "Run Agent Investigation").disabled
    saved_before = deepcopy(app.session_state["ui_batch_selection"]["saved"])
    button(app, "Run Agent Investigation").click().run()
    assert not app.exception
    runner.assert_called_once_with("txn_samplepass", "2022-09-14", window_h=48)
    assert view(app).value == "On-Demand Brief"
    assert "Unsaved single-alert result" in content(app)
    app.run()
    next(widget for widget in app.button if widget.key and widget.key.startswith(
        "ui_on_demand_display_evidence_finding_0_"
    )).click().run()
    assert "The exact transaction is highlighted" in content(app)
    view(app).set_value("Saved Batch Brief").run()
    assert "Saved run:" in content(app)
    app.button(key="ui_evidence_finding_0_0").click().run()
    view(app).set_value("On-Demand Brief").run()
    runner.assert_called_once()
    assert backend.load_queue.call_count == backend.open.call_count == 1
    assert app.session_state["ui_batch_selection"]["saved"] == saved_before
    button(app, "Run again").click().run()
    assert runner.call_count == 2
    assert not app.exception


def test_no_saved_decision_still_permits_explicit_live_investigation():
    result = deepcopy(sample_investigations()["not_found"])
    result["txn_id"] = result["alert"]["txn_id"] = "txn_demounprocessed"
    app, runner, _ = setup_live("txn_demounprocessed", Mock(return_value=result))
    assert "No batch result is stored" in content(app)
    assert not app.get("iframe")
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    assert "Transaction not found." in content(app)
    assert view(app).options == ["On-Demand Brief"]
    runner.assert_called_once_with("txn_demounprocessed", "2022-09-14", window_h=48)
    assert app.session_state["ui_batch_selection"]["saved"].result is None
    assert not app.exception


def test_changed_window_hides_stale_result_and_requires_new_approval():
    app, runner, _ = setup_live()
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    window(app).set_value(24).run()
    assert view(app).options == ["Saved Batch Brief"]
    assert button(app, "Run Agent Investigation").disabled
    assert "Unsaved single-alert result" not in content(app)
    runner.assert_called_once()
    window(app).set_value(48).run()
    assert "On-Demand Brief" in view(app).options
    runner.assert_called_once()


def test_target_source_and_date_do_not_reuse_unrelated_results():
    app, runner, _ = setup_live()
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    app.button[0].click().run()
    open_alert(app, "txn_samplereturn")
    assert view(app).options == ["Saved Batch Brief"]
    runner.assert_called_once()
    app.button[0].click().run()
    app.selectbox(key="ui_batch_day").set_value("2022-09-16")
    button(app, "Load / refresh queue").click().run()
    open_alert(app, "txn_samplequiet")
    assert view(app).options == ["Saved Batch Brief"]
    app.radio(key="ui_batch_source").set_value(DEMO_SOURCE).run()
    assert "Unsaved single-alert result" not in content(app)
    assert "Evidence view" not in [widget.label for widget in app.radio]
    runner.assert_called_once()


def test_saved_grouped_case_is_not_used_as_single_alert_on_demand_evidence():
    single = deepcopy(sample_investigations()["pass_through"])
    # A stand-in for the real single-alert API response, independent of the saved case.
    single["txn_id"] = single["alert"]["txn_id"] = single["members"][0]["graph"]["txn_id"] = "txn_samplefanmember"
    single["money_flow"][0]["txn_id"] = "txn_samplefanmember"
    single["window_h"] = 24
    app, runner, _ = setup_live("txn_samplefanmember", Mock(return_value=single))
    assert app.selectbox(key="ui_batch_member").value == "txn_samplefanmember"
    window(app).set_value(24).run()
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    assert not app.selectbox
    assert "Representative alert: txn_samplefan" not in content(app)
    assert "Selected alert · 1 supported findings" in content(app)
    view(app).set_value("Saved Batch Brief").run()
    assert "Overall case · 4 supported findings" in content(app)
    assert "Original saved investigation window: ±48 hours" in content(app)
    assert len(app.selectbox(key="ui_batch_member").options) == 2
    view(app).set_value("On-Demand Brief").run()
    assert "Requested window: ±24 hours" in content(app)
    runner.assert_called_once_with("txn_samplefanmember", "2022-09-14", window_h=24)


@pytest.mark.parametrize("error", [NoCredentialsError(), ClientError({"Error": {
    "Code": "AccessDeniedException", "Message": "SECRET-DO-NOT-DISPLAY",
}}, "Converse"), RuntimeError("SECRET-DO-NOT-DISPLAY")])
def test_failure_preserves_saved_evidence_and_does_not_retry(error):
    app, runner, _ = setup_live(runner=Mock(side_effect=error))
    saved = deepcopy(app.session_state["ui_batch_selection"]["saved"])
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    assert app.error
    assert "SECRET-DO-NOT-DISPLAY" not in content(app)
    assert view(app).options == ["Saved Batch Brief"]
    assert app.session_state["ui_batch_selection"]["saved"] == saved
    app.run()
    runner.assert_called_once()
    assert button(app, "Run again")


@pytest.mark.parametrize(("scenario", "expected"), [
    ("no_graph", "No supported finding was identified"),
    ("repaired", "Accepted after repair"), ("fallback", "Code-only fallback"),
])
def test_demo_simulation_uses_shared_renderer_and_stays_offline(scenario, expected):
    fixture = sample_investigations()[scenario]
    app = open_alert(dashboard(), fixture["txn_id"])
    button(app, "Run Agent Investigation").click().run()
    assert not app.exception
    assert "SIMULATED ON-DEMAND RESULT" in content(app)
    assert expected in content(app)
    assert not app.checkbox


def test_invented_demo_alert_reports_no_simulation_and_never_calls_real_agent():
    app = open_alert(dashboard(), "txn_demounprocessed")
    button(app, "Run Agent Investigation").click().run()
    assert "No simulated single-alert result is available" in content(app)
    assert "Evidence view" not in [widget.label for widget in app.radio]
    assert not app.exception


def test_bad_runner_identity_is_rejected_instead_of_showing_another_alert():
    app, runner, _ = setup_live(runner=Mock(return_value=sample_investigations()["money_returns"]))
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    assert view(app).options == ["Saved Batch Brief"]
    assert app.error
    runner.assert_called_once()


@pytest.mark.parametrize(("error", "category"), [
    (NoCredentialsError(), "Authentication failure"),
    (ClientError({"Error": {"Code": "ExpiredToken", "Message": "secret"}}, "GetItem"), "Authentication failure"),
    (ClientError({"Error": {"Code": "AccessDeniedException", "Message": "secret"}}, "GetItem"), "Insufficient permissions"),
    (AthenaQueryError("TABLE_NOT_FOUND secret"), "Missing data table or resource"),
    (ReadTimeoutError(endpoint_url="https://private.invalid"), "Transient service failure"),
])
def test_safe_failure_categories_keep_exceptions_private(error, category):
    failure = describe_failure(error, "Queue load")
    assert failure.category == category and failure.cause is error
    assert "secret" not in failure.message and "private.invalid" not in failure.message
    assert "private.invalid" not in repr(failure)


def test_live_athena_table_error_is_safe_and_not_an_empty_queue():
    backend = Mock()
    backend.load_queue.side_effect = AthenaQueryError("TABLE_NOT_FOUND secret table response")
    app = dashboard(backend, LIVE_SOURCE)
    button(app, "Load / refresh queue").click().run()
    assert "Missing data table or resource" in content(app)
    assert "secret table response" not in content(app)
    assert "No alerts are available" not in content(app)


def test_decision_authentication_error_reaches_opened_alert_without_becoming_missing(monkeypatch):
    resource = FakeDynamo()
    monkeypatch.setattr(resource, "batch_get_item", Mock(side_effect=NoCredentialsError()))
    reader = SavedResultsReader(resource)
    batch = reader.read_decisions(["txn_a"])
    saved = reader.open("txn_a", "2022-09-14", batch)
    assert saved.status == "error" and "Authentication failure" in saved.detail
    assert saved.failure.cause is batch.errors["txn_a"].cause


def test_passive_member_and_trace_navigation_never_executes_agent():
    app, runner, _ = setup_live("txn_samplefanmember")
    app.selectbox(key="ui_batch_member").set_value("txn_samplefan").run()
    app.button(key="ui_evidence_finding_0_0").click().run()
    app.run()
    runner.assert_not_called()


def test_in_progress_session_disables_duplicate_execution():
    app, runner, _ = setup_live()
    approve(app)
    app.session_state["ui_on_demand_running"] = (LIVE_SOURCE, "txn_a", "2022-09-14", 48)
    app.run()
    assert button(app, "Run Agent Investigation").disabled
    runner.assert_not_called()


def test_on_demand_currency_metrics_and_trace_only_evidence_are_independent():
    result = deepcopy(sample_investigations()["pass_through"])
    result["money_flow"][0]["per_currency"]["Euro"] = {
        "upstream_total": 10.0, "downstream_total": 20.0, "quick_onward_share": None,
        "fastest_onward_gap_minutes": None, "median_onward_gap_minutes": None,
        "fastest_onward_txn_id": None,
    }
    result["findings"][0]["evidence_ids"].append("txn_traceonly")
    result["trace"].append({
        "step": 99, "actor": "code", "tool": "get_account_activity", "inputs": {},
        "result": {"incoming": [{"txn_id": "txn_traceonly", "amount": 10, "currency": "Euro"}]},
        "latency_ms": 0,
    })
    app, runner, _ = setup_live(runner=Mock(return_value=result))
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    assert app.dataframe[0].value["Currency"].tolist() == ["US Dollar", "Euro"]
    button(app, "txn_traceonly").click().run()
    assert "outside the displayed money-flow graph" in content(app)
    view(app).set_value("Saved Batch Brief").run()
    assert app.dataframe[0].value["Currency"].tolist() == ["US Dollar"]
    assert "txn_traceonly" not in content(app)
    runner.assert_called_once()


def test_failed_explicit_rerun_cannot_fabricate_success_or_replace_saved_data():
    runner = Mock(side_effect=[sample_investigations()["pass_through"], ReadTimeoutError(endpoint_url="https://private.invalid")])
    app, _, _ = setup_live(runner=runner)
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    saved = app.session_state["ui_batch_selection"]["saved"]
    button(app, "Run again").click().run()
    assert view(app).options == ["Saved Batch Brief"]
    assert "Transient service failure" in content(app)
    assert "private.invalid" not in content(app)
    assert app.session_state["ui_batch_selection"]["saved"] is saved
    app.run()
    assert runner.call_count == 2


def test_malformed_legacy_pointer_remains_incompatible_instead_of_crashing():
    batch = DecisionBatch({"txn_old": {"transaction_id": "txn_old", "representative_txn_id": []}})
    saved = SavedResultsReader().open("txn_old", "2022-09-14", batch)
    assert saved.status == "incompatible"


def test_saved_member_and_highlight_survive_on_demand_view_switch():
    single = deepcopy(sample_investigations()["pass_through"])
    single["txn_id"] = single["alert"]["txn_id"] = single["members"][0]["graph"]["txn_id"] = "txn_samplefanmember"
    single["money_flow"][0]["txn_id"] = "txn_samplefanmember"
    app, runner, _ = setup_live("txn_samplefanmember", Mock(return_value=single))
    button(app, "txn_samplefanup").click().run()
    assert app.selectbox(key="ui_batch_member").value == "txn_samplefan"
    approve(app)
    button(app, "Run Agent Investigation").click().run()
    assert view(app).value == "On-Demand Brief"
    view(app).set_value("Saved Batch Brief").run()
    assert app.selectbox(key="ui_batch_member").value == "txn_samplefan"
    assert app.session_state["ui_batch_evidence"] == "txn_samplefanup"
    assert "The exact transaction is highlighted" in content(app)
    runner.assert_called_once()
