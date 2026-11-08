"""Queue-to-case, graph and evidence navigation with guarded Streamlit execution."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from src.ui.pages.batch import DEMO_SOURCE, LIVE_SOURCE
from src.ui.services.demo import DemoBatchService
from src.ui.services.saved_results import SavedBrief

APP = Path(__file__).resolve().parents[2] / "app.py"


def content(app):
    return "\n".join(str(element.value) for kind in (
        "text", "caption", "info", "warning", "error", "markdown", "subheader", "code",
    ) for element in app.get(kind))


def dashboard(backend=None, source=DEMO_SOURCE):
    app = AppTest.from_file(APP, default_timeout=10)
    if backend is not None:
        app.session_state["ui_batch_backends"] = {source: backend}
    app.session_state["ui_batch_source"] = source
    app.run()
    assert not app.exception
    return app


def open_alert(app, txn_id):
    app.selectbox(key="ui_batch_queue_alert").set_value(txn_id)
    next(button for button in app.button if button.label == "Open Investigation").click().run()
    assert not app.exception
    assert app.radio(key="ui_navigation_page").value == "Investigation"
    return app


def test_default_demo_queue_retains_original_rank_and_all_availability_states():
    app = dashboard()
    table = app.dataframe[0].value
    assert table["BQ1 daily rank"].tolist() == sorted(table["BQ1 daily rank"].tolist())
    assert {"No saved brief", "Saved · no findings", "Saved · code-only fallback"} <= set(table["Brief availability"])
    assert "txn_demounprocessed" in set(table["Transaction ID"])
    assert "not Athena results" in content(app)
    for excluded in ("evaluation_is_laundering", "data_split", "confidence", "verdict"):
        assert excluded not in content(app) and excluded not in table.columns


def test_open_member_preserves_selected_alert_and_representative_identity():
    app = open_alert(dashboard(), "txn_samplefanmember")
    assert app.selectbox(key="ui_batch_member").value == "txn_samplefanmember"
    assert "Opened queue alert: txn_samplefanmember" in content(app)
    assert "Representative alert: txn_samplefan" in content(app)
    assert "Transaction ID: txn_samplefanmember" in content(app)
    assert "Overall case · 4 supported findings" in content(app)
    assert app.get("iframe")  # Bundled native graph is embedded.
    app.selectbox(key="ui_batch_member").set_value("txn_samplefan").run()
    assert "Transaction ID: txn_samplefan" in content(app)
    assert not app.exception


def test_saved_case_reruns_do_not_read_again_and_graph_reference_highlights():
    backend = DemoBatchService()
    backend.load_queue = Mock(wraps=backend.load_queue)
    backend.open = Mock(wraps=backend.open)
    app = open_alert(dashboard(backend), "txn_samplepass")
    app.button(key="ui_evidence_finding_0_0").click().run()
    assert app.session_state["ui_batch_evidence"] == "txn_samplepassdown1"
    assert "The exact transaction is highlighted" in content(app)
    app.run()
    assert backend.load_queue.call_count == backend.open.call_count == 1
    assert not app.exception


def test_trace_only_evidence_is_shown_without_inventing_graph_edge():
    app = open_alert(dashboard(), "txn_samplefan")
    app.button(key="ui_evidence_finding_0_0").click().run()
    assert "outside the displayed money-flow graph" in content(app)
    assert "get_account_activity" in content(app)
    assert app.session_state["ui_batch_member"] == "txn_samplefan"
    assert not app.exception


def test_evidence_can_navigate_to_a_different_member_graph():
    backend = DemoBatchService()
    key = backend.rows["txn_samplefan"]["trace_key"]
    backend.envelopes[key]["result"]["findings"][0]["evidence_ids"].append("txn_samplefanmemberdown")
    app = open_alert(dashboard(backend), "txn_samplefan")
    button = next(button for button in app.button if button.label == "txn_samplefanmemberdown")
    button.click().run()
    assert app.selectbox(key="ui_batch_member").value == "txn_samplefanmember"
    assert "Transaction ID: txn_samplefanmember" in content(app)
    assert "The exact transaction is highlighted" in content(app)


@pytest.mark.parametrize(("capped", "expected"), [
    (False, "Unresolved reference"), (True, "A graph is capped"),
])
def test_unresolved_and_capped_evidence_have_explicit_limitations(capped, expected):
    backend = DemoBatchService()
    key = backend.rows["txn_samplepass"]["trace_key"]
    result = backend.envelopes[key]["result"]
    result["findings"][0]["evidence_ids"].append("txn_unavailable")
    result["members"][0]["graph"]["edges_capped"] = capped
    app = open_alert(dashboard(backend), "txn_samplepass")
    next(button for button in app.button if button.label == "txn_unavailable").click().run()
    assert expected in content(app)
    assert not app.exception


@pytest.mark.parametrize(("txn_id", "expected"), [
    ("txn_demounprocessed", "No batch result is stored"),
    ("txn_samplenograph", "No supported finding was identified"),
    ("txn_samplefallback", "Code-only fallback"),
    ("txn_samplecodeonly", "Code-only demonstration"),
])
def test_missing_empty_fallback_and_code_only_are_distinct(txn_id, expected):
    app = open_alert(dashboard(), txn_id)
    assert expected in content(app)
    if txn_id in {"txn_demounprocessed", "txn_samplenograph"}:
        assert not app.get("iframe")


def test_date_and_top_k_controls_and_truncated_graph():
    app = dashboard()
    app.selectbox(key="ui_batch_day").set_value("2022-09-16")
    app.number_input(key="ui_batch_top_k").set_value(100)
    app.button[0].click().run()
    assert app.dataframe[0].value["Transaction ID"].tolist() == ["txn_samplequiet"]
    open_alert(app, "txn_samplequiet")
    assert "extends beyond the dataset" in content(app)
    expected = DemoBatchService().results["quiet_truncated"]["gaps"]
    assert all(gap["sentence"] in content(app) for gap in expected)
    app.button[0].click().run()
    assert app.selectbox(key="ui_batch_day").value == "2022-09-16"
    assert app.number_input(key="ui_batch_top_k").value == 100


def test_live_source_requires_explicit_load_and_never_substitutes_demo():
    app = dashboard(source=LIVE_SOURCE)
    assert not app.dataframe
    assert "Load a queue" in content(app)
    assert "ui_batch_backends" not in app.session_state
    assert "DEMO DATA" not in content(app)
    assert not app.exception


def test_live_failure_stays_live_and_does_not_show_sample_queue():
    backend = Mock()
    backend.load_queue.side_effect = ValueError("fake unavailable queue")
    app = dashboard(backend, LIVE_SOURCE)
    app.button[0].click().run()
    assert "Queue read failed" in content(app)
    assert app.radio(key="ui_batch_source").value == LIVE_SOURCE
    assert not app.dataframe
    assert "txn_sample" not in content(app)


@pytest.mark.parametrize("status", ["error", "incompatible"])
def test_saved_read_failures_and_incompatible_records_are_visible(status):
    backend = DemoBatchService()
    backend.open = Mock(return_value=SavedBrief(status, f"Saved {status} test"))
    app = open_alert(dashboard(backend), "txn_samplepass")
    assert f"Saved {status} test" in content(app)
    assert not app.get("iframe")
    app.button[0].click().run()
    status_label = "Read failed" if status == "error" else "Incompatible saved data"
    assert status_label in set(app.dataframe[0].value["Brief availability"])


def test_live_source_is_retained_across_legacy_navigation():
    app = dashboard(source=LIVE_SOURCE)
    app.radio(key="ui_navigation_page").set_value("BQ2 Money Trail (Legacy)").run()
    app.radio(key="ui_navigation_page").set_value("Alert Queue").run()
    assert app.radio(key="ui_batch_source").value == LIVE_SOURCE
    assert not app.dataframe
    assert "DEMO DATA" not in content(app)


def test_navigation_keeps_legacy_investigation_state_untouched():
    app = dashboard()
    sentinel = {"transaction_id": "legacy-sentinel"}
    app.session_state["investigation"] = deepcopy(sentinel)
    open_alert(app, "txn_samplepass")
    app.radio(key="ui_navigation_page").set_value("BQ2 Money Trail (Legacy)").run()
    assert app.title[0].value == "AML Transaction Network"
    assert app.session_state["investigation"] == sentinel
    assert not app.exception
