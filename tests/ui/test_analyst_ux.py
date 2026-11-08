"""Professor-facing workflow regressions; UI fixtures prohibit all real AWS/agent calls."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from src.agent.sample_results import sample_investigations
from src.ui.components.agent_graph import build_network, graph_transactions
from src.ui.components.evidence import evidence_fields, resolve_evidence
from src.ui.pages.batch import DEMO_SOURCE, LIVE_SOURCE
from src.ui.pages.queue import STALE_ALERT, open_investigation, select_queue_row
from src.ui.services.demo import DemoBatchService
from tests.ui.test_batch_dashboard import APP, content, dashboard, open_alert


def button(app, label):
    return next(widget for widget in app.button if widget.label == label)


def load(app, day=None, top_k=None):
    if day:
        app.selectbox(key="ui_batch_day").set_value(day)
    if top_k:
        app.number_input(key="ui_batch_top_k").set_value(top_k)
    button(app, "Load / refresh queue").click().run()
    assert not app.exception
    return app


@pytest.mark.parametrize(("first", "selected", "second"), [
    ("2022-09-16", "txn_samplequiet", "2022-09-14"),
    ("2022-09-14", "txn_samplepass", "2022-09-16"),
])
def test_date_change_replaces_selection_before_widgets(first, selected, second):
    app = load(dashboard(), first)
    app.selectbox(key="ui_batch_queue_alert").set_value(selected).run()
    load(app, second)
    active_ids = app.dataframe[0].value["Transaction ID"].tolist()
    assert selected not in active_ids
    assert app.selectbox(key="ui_batch_queue_alert").value in active_ids
    button(app, "Open Selected Investigation").click().run()
    assert not app.exception
    assert app.session_state["ui_batch_selection"]["txn_id"] in active_ids


def test_top_k_removal_and_reload_replace_row_selection():
    app = dashboard()
    app.selectbox(key="ui_batch_queue_alert").set_value("txn_demounprocessed").run()
    generation = app.session_state["ui_batch_generation"]
    load(app, top_k=1)
    assert app.selectbox(key="ui_batch_queue_alert").value == app.dataframe[0].value.iloc[0]["Transaction ID"]
    assert app.session_state["ui_batch_generation"] == generation + 1
    load(app)
    assert app.session_state["ui_batch_generation"] == generation + 2
    button(app, "Open Selected Investigation").click().run()
    assert not app.exception


def test_source_change_clears_demo_selection_and_live_defaults_to_twenty():
    demo = DemoBatchService()
    live = Mock(wraps=DemoBatchService())
    app = dashboard(demo)
    app.session_state["ui_batch_backends"] = {DEMO_SOURCE: demo, LIVE_SOURCE: live}
    app.selectbox(key="ui_batch_queue_alert").set_value("txn_demounprocessed").run()
    app.radio(key="ui_batch_source").set_value(LIVE_SOURCE).run()
    assert not app.dataframe
    assert not [widget for widget in app.selectbox if widget.key == "ui_batch_queue_alert"]
    assert app.number_input(key="ui_batch_top_k").value == 20
    live.load_queue.assert_not_called()
    load(app, top_k=1)
    button(app, "Open Selected Investigation").click().run()
    assert not app.exception
    live.open.assert_called_once()
    assert app.session_state["ui_batch_selection"]["source"] == LIVE_SOURCE


@pytest.mark.parametrize("source_matches", [True, False])
def test_invalid_callback_preserves_queue_and_never_reads(monkeypatch, source_matches):
    backend = Mock()
    snapshot = DemoBatchService().load_queue("2022-09-14", 100)
    cached = {"source": DEMO_SOURCE, "snapshot": snapshot}
    state = {"ui_batch_snapshot": cached, "ui_batch_queue_alert": "txn_samplequiet",
             "ui_navigation_page": "Alert Queue"}
    monkeypatch.setattr("src.ui.pages.queue.st.session_state", state)
    monkeypatch.setattr("src.ui.pages.queue.get_backend", backend)
    open_investigation(DEMO_SOURCE if source_matches else LIVE_SOURCE)
    backend.assert_not_called()
    assert state["ui_batch_snapshot"] is cached
    assert state["ui_navigation_page"] == "Alert Queue"
    assert state["ui_batch_selection_notice"] == STALE_ALERT
    assert "ui_batch_selection" not in state


def test_stale_selection_notice_is_visible_and_queue_is_retained():
    backend = DemoBatchService()
    snapshot = backend.load_queue("2022-09-14", 20)
    app = AppTest.from_file(APP)
    app.session_state["ui_batch_controls_source"] = DEMO_SOURCE
    app.session_state["ui_batch_snapshot"] = {"source": DEMO_SOURCE, "snapshot": snapshot}
    app.session_state["ui_batch_selection_notice"] = STALE_ALERT
    app.session_state["ui_batch_queue_alert"] = "txn_gone"
    app.run()
    assert STALE_ALERT in content(app)
    assert app.dataframe[0].value["Transaction ID"].tolist() == [entry["txn_id"] for entry in snapshot.entries]
    assert app.selectbox(key="ui_batch_queue_alert").value in [entry["txn_id"] for entry in snapshot.entries]
    assert not app.exception


def test_empty_queue_has_no_selector_and_disabled_open():
    app = load(dashboard(), "2022-09-15")
    assert not app.dataframe
    assert not [widget for widget in app.selectbox if widget.key == "ui_batch_queue_alert"]
    assert button(app, "Open Selected Investigation").disabled
    assert "No alerts are available" in content(app)


@pytest.mark.parametrize("row", [0, 2, -1, 1000])
def test_table_row_event_uses_current_snapshot_and_ignores_invalid_rows(monkeypatch, row):
    snapshot = DemoBatchService().load_queue("2022-09-14", 100)
    state = {"ui_batch_snapshot": {"source": DEMO_SOURCE, "snapshot": snapshot}, "ui_batch_generation": 2,
             "table": {"selection": {"rows": [row]}}, "ui_batch_queue_alert": "unchanged"}
    monkeypatch.setattr("src.ui.pages.queue.st.session_state", state)
    select_queue_row("table", DEMO_SOURCE, 1)
    assert state["ui_batch_queue_alert"] == "unchanged"
    select_queue_row("table", DEMO_SOURCE, 2)
    assert state["ui_batch_queue_alert"] == (snapshot.entries[row]["txn_id"] if 0 <= row < len(snapshot.entries) else "unchanged")


def test_queue_has_six_columns_in_original_order_and_optional_secondary_fields():
    app = dashboard()
    assert list(app.dataframe[0].value.columns) == [
        "Daily rank", "Transaction ID", "Risk score", "Transaction time (UTC)", "Brief status", "Finding count",
    ]
    table = app.dataframe[0].value
    assert table.loc[table["Transaction ID"] == "txn_demounprocessed", "Finding count"].item() == "—"
    expected = DemoBatchService().load_queue("2022-09-14", 5000)
    assert app.dataframe[0].value["Transaction ID"].tolist() == [row["txn_id"] for row in expected.entries]
    assert "How to use this dashboard" in [widget.label for widget in app.expander]
    assert "Finding codes:" in content(app)


def test_payment_card_updates_with_actual_values_and_never_reloads_services():
    backend = DemoBatchService()
    backend.open = Mock(wraps=backend.open)
    backend.load_queue = Mock(wraps=backend.load_queue)
    app = open_alert(dashboard(backend), "txn_samplepass")
    result = backend.results["pass_through"]
    assert result["summary"] in content(app)
    button(app, "txn_samplepassdown1").click().run()
    text = content(app)
    assert "US$95,000" in text
    assert "200|SAMPLE2 → 900|SAMPLEPASSDOWN1" in text
    assert "14 Sep 2022, 10:25:00 UTC" in text
    assert "Time after flagged transaction: 25 minutes" in text
    assert "PASS_THROUGH" in text
    assert "Rapid onward transfers" in text
    raw = next(widget for widget in app.expander if widget.label == "Raw evidence details")
    assert not raw.proto.expanded
    button(app, "txn_samplepassdown2").click().run()
    assert app.session_state["ui_batch_evidence"] == "txn_samplepassdown2"
    record = resolve_evidence(result, "txn_samplepassdown2").record
    assert f"{record['gap_minutes']:g} minutes" in content(app)
    app.run()
    backend.open.assert_called_once()
    backend.load_queue.assert_called_once()
    assert not app.exception


def test_trace_only_card_uses_explicit_account_direction_and_offset():
    app = open_alert(dashboard(), "txn_samplefan")
    button(app, "txn_samplefanout1").click().run()
    assert "100|SAMPLE1 → 950|FANOUT1" in content(app)
    assert "US$4,000" in content(app)
    assert "Time after flagged transaction: 10 minutes" in content(app)
    reference = resolve_evidence(sample_investigations()["fan_out_case"], "txn_samplefanout1")
    assert f"Trace step {reference.step} · get_account_activity" in content(app)
    assert "outside the displayed money-flow graph" in content(app)
    assert not app.exception


@pytest.mark.parametrize("evidence_id", ["txn_samplefanout1", "txn_samplefanin1", "txn_samplerelated"])
def test_trace_mapping_does_not_fabricate_missing_fields(evidence_id):
    result = sample_investigations()["fan_out_case"]
    before = deepcopy(result)
    reference = resolve_evidence(result, evidence_id)
    fields = evidence_fields(reference)
    if evidence_id == "txn_samplefanin1":
        assert fields["sender"] == "960|FANIN1" and fields["receiver"] == "200|SAMPLE2"
        assert fields["offset_minutes"] == -10
    if evidence_id == "txn_samplerelated":
        assert fields["receiver"] == "100|SAMPLE1"
        assert fields["sender"] is None and fields["amount"] is None
        assert "offset_minutes" not in fields
    assert result == before


def test_graph_has_stable_readable_positions_and_selected_text_for_small_and_large_cases():
    small = sample_investigations()["pass_through"]["members"][0]
    large = deepcopy(small)
    for index in range(30):
        large["graph"]["downstream"].append({**small["graph"]["downstream"][0],
                                            "txn_id": f"txn_large{index}", "receiver": f"900|LARGE{index}"})
    for member in (small, large):
        network = build_network(member, selected_id="txn_samplepassdown1")
        repeated = build_network(member, selected_id="txn_samplepassdown1")
        assert network.nodes == repeated.nodes and network.edges == repeated.edges
        assert len({(node["x"], node["y"]) for node in network.nodes}) == len(network.nodes)
        assert all(node["font"]["size"] >= 17 for node in network.nodes)
        assert {edge["id"] for edge in network.edges} == {payment["txn_id"] for payment in graph_transactions(member)}
        selected = next(edge for edge in network.edges if edge["id"] == "txn_samplepassdown1")
        assert selected["label"] == "Selected" and selected["width"] == 6
        anchor = next(edge for edge in network.edges if edge["id"] == member["alert"]["txn_id"])
        assert anchor["label"] == "Flagged transfer" and anchor["width"] >= 4


def test_investigation_hierarchy_and_advanced_run_are_secondary():
    app = open_alert(dashboard(), "txn_samplepass")
    headings = [heading.value for heading in app.subheader]
    assert headings.index("Investigation summary") < headings.index("Validated findings") < headings.index("Money-flow graph")
    assert headings.index("Selected transaction details") < headings.index("Additional analysis") < headings.index("Technical details")
    expander = next(widget for widget in app.expander if widget.label == "Advanced: Run a new AI investigation")
    assert not expander.proto.expanded
    assert "ui_on_demand_services" not in app.session_state
    assert not app.session_state["ui_on_demand_attempts"]
    assert not app.exception
