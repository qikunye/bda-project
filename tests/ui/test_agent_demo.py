"""Offline UI checks: every AWS and real-agent boundary fails if used."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.agent.sample_results import sample_investigations
from src.ui.components.agent_brief import UNAVAILABLE, money_flow_rows
from src.ui.pages import agent_demo

APP = Path(__file__).resolve().parents[2] / "app.py"
LEGACY = "BQ2 Money Trail (Legacy)"
DEMO = "Agent Evidence Brief (Demo)"
SAMPLES = sample_investigations()




def demo(scenario="pass_through"):
    app = AppTest.from_file(str(APP), default_timeout=10)
    app.session_state["ui_navigation_page"] = DEMO
    app.run()
    if scenario != "pass_through":
        app.selectbox(key="agent_demo_scenario").set_value(scenario).run()
    assert not app.exception
    return app


def visible_text(app):
    return "\n".join(
        str(element.value)
        for kind in ("title", "subheader", "markdown", "text", "caption", "info", "warning", "code")
        for element in app.get(kind)
    )


def metric(app, label):
    return next(item.value for item in app.metric if item.label == label)


@pytest.mark.parametrize("scenario", SAMPLES)
def test_every_scenario_renders_supplied_content_without_aws(scenario):
    app = demo(scenario)
    result = SAMPLES[scenario]
    content = visible_text(app)
    assert agent_demo.DEMO_NOTICE in [item.value for item in app.warning]
    assert "No AWS services or Bedrock models are called." in content
    assert "Full analysis days end on 16 Sep 2022." in content
    assert app.selectbox(key="agent_demo_scenario").options == list(agent_demo.SCENARIO_LABELS.values())
    assert LEGACY in app.radio(key="ui_navigation_page").options
    assert "investigation" not in app.session_state
    assert not app.button  # No action can accidentally execute the agent.
    for forbidden_label in ("verdict", "confidence", "model reasons"):
        assert forbidden_label not in content.lower()

    if result["status"] == "not_found":
        assert "Transaction not found." in content
        assert not app.metric and not app.expander and not app.dataframe
        assert "Selected alert" not in content
        return

    assert result["summary"] in content
    assert f"{len(result['findings'])} supported findings" in content
    for finding in result["findings"]:
        assert finding["sentence"] in content
        for evidence_id in finding["evidence_ids"]:
            assert evidence_id in content
    for gap in result["gaps"]:
        assert gap["sentence"] in [item.value for item in app.warning]
    for check in result["next_checks"]:
        assert f"- [ ] {check['sentence']}" in [item.value for item in app.markdown]
    for observation in result["observations"]:
        assert observation["text"] in content
        assert all(evidence_id in content for evidence_id in observation["evidence_ids"])
    assert "Agent observations — interpretation not independently verified" in content
    if not result["findings"]:
        assert "No supported finding was identified in the available evidence." in content
    if scenario == "no_graph":
        assert f"Amount: {UNAVAILABLE}" in content
        assert f"Currency: {UNAVAILABLE}" in content
        assert f"Payment format: {UNAVAILABLE}" in content
        assert metric(app, "Upstream transactions") == "0"
        assert not app.dataframe
    else:
        flow = result["money_flow"][0]
        assert app.dataframe[0].value.to_dict("records") == money_flow_rows(flow)
    assert "not a laundering probability" in content


def test_scenario_switches_reset_member_selection_and_preserve_legacy_state():
    app = demo("fan_out_case")
    sentinel = {"transaction_id": "legacy-sentinel", "window_hours": 24}
    app.session_state["investigation"] = deepcopy(sentinel)
    app.selectbox(key="agent_demo_member_fan_out_case").set_value("txn_samplefanmember").run()
    assert "Transaction ID: txn_samplefanmember" in visible_text(app)
    assert metric(app, "BQ1 daily rank") == "8"
    assert metric(app, "Upstream transactions") == "0"
    assert metric(app, "Downstream transactions") == "1"
    assert app.dataframe[0].value.iloc[0]["Downstream total"] == "1,000.00"
    assert "Overall case · 4 supported findings" in visible_text(app)
    assert SAMPLES["fan_out_case"]["summary"] in visible_text(app)

    for scenario in SAMPLES:
        app.selectbox(key="agent_demo_scenario").set_value(scenario).run()
        assert not app.exception
        assert agent_demo.DEMO_NOTICE in visible_text(app)
        assert app.session_state["investigation"] == sentinel
        if scenario != "fan_out_case":
            assert len(app.selectbox) == 1
    app.selectbox(key="agent_demo_scenario").set_value("fan_out_case").run()
    assert app.selectbox(key="agent_demo_member_fan_out_case").value == "txn_samplefan"


@pytest.mark.parametrize(("scenario", "notice"), [
    ("code_only", "Code-only demonstration — fixed rules built this brief; this is not a fallback."),
    ("repaired", "Accepted after repair — the brief passed after one retry."),
    ("fallback", "Code-only fallback — fixed rules produced the brief after LLM failure."),
])
def test_execution_provenance_is_explicit(scenario, notice):
    app = demo(scenario)
    assert notice in visible_text(app)
    assert json.loads(app.json[0].value) == SAMPLES[scenario]["stats"]
    assert all(not expander.proto.expanded for expander in app.expander)
    assert [expander.label for expander in app.expander] == [
        "How this brief was generated", "Tool trace",
    ]
    trace = SAMPLES[scenario]["trace"]
    assert len(app.json) == 1 + 2 * len(trace)
    for i, step in enumerate(trace):
        assert f"Step {step['step']} · {step['actor']} · {step['tool']}" in visible_text(app)
        assert json.loads(app.json[1 + i * 2].value) == step["inputs"]
        assert json.loads(app.json[2 + i * 2].value) == step["result"]
    if scenario == "code_only":
        assert not any("Code-only fallback" in warning.value for warning in app.warning)


def test_observations_are_separate_and_do_not_increase_finding_count():
    app = demo("fan_out_case")
    headings = [item.value for item in app.subheader]
    assert headings.index("Validated findings") < headings.index(
        "Agent observations — interpretation not independently verified"
    )
    assert "Overall case · 4 supported findings" in visible_text(app)
    assert "5 supported findings" not in visible_text(app)


def test_currency_rows_preserve_null_zero_and_ratios_above_one(monkeypatch):
    results = deepcopy(SAMPLES)
    flow = results["pass_through"]["money_flow"][0]
    usd = flow["per_currency"]["US Dollar"]
    usd["quick_onward_share"] = 2.5
    flow["per_currency"]["Euro"] = {
        "upstream_total": 0.0,
        "downstream_total": 17.25,
        "quick_onward_share": None,
        "fastest_onward_gap_minutes": None,
        "median_onward_gap_minutes": None,
        "fastest_onward_txn_id": None,
    }
    before = deepcopy(results)
    monkeypatch.setattr(agent_demo, "sample_investigations", lambda: results)
    app = demo()
    rows = app.dataframe[0].value.to_dict("records")
    assert len(rows) == 2
    assert rows[0]["Currency"] == "US Dollar"
    assert rows[0]["Quick onward share"] == "2.50× alert amount"
    assert rows[1]["Currency"] == "Euro"
    assert rows[1]["Upstream total"] == "0.00"
    assert rows[1]["Downstream total"] == "17.25"
    assert rows[1]["Quick onward share"] == UNAVAILABLE
    assert rows[1]["Fastest onward (minutes)"] == UNAVAILABLE
    assert "200,017.25" not in str(rows)
    assert results == before


def test_member_flow_matches_by_id_even_when_flow_order_changes(monkeypatch):
    results = deepcopy(SAMPLES)
    results["fan_out_case"]["money_flow"].reverse()
    before = deepcopy(results)
    monkeypatch.setattr(agent_demo, "sample_investigations", lambda: results)
    app = demo("fan_out_case")
    assert metric(app, "Upstream transactions") == "1"
    app.selectbox(key="agent_demo_member_fan_out_case").set_value("txn_samplefanmember").run()
    assert metric(app, "Upstream transactions") == "0"
    assert results == before


def test_missing_money_flow_is_not_displayed_as_zero(monkeypatch):
    results = deepcopy(SAMPLES)
    results["pass_through"]["money_flow"] = []
    monkeypatch.setattr(agent_demo, "sample_investigations", lambda: results)
    app = demo()
    assert "Money-flow metrics are unavailable for this transaction." in visible_text(app)
    assert not app.dataframe
    assert "Upstream transactions" not in [item.label for item in app.metric]


def test_legacy_initial_controls_and_navigation_remain_available():
    app = AppTest.from_file(str(APP), default_timeout=10)
    app.session_state["ui_navigation_page"] = LEGACY
    app.run()
    assert not app.exception
    assert app.radio(key="ui_navigation_page").value == LEGACY
    assert app.title[0].value == "AML Transaction Network"
    assert app.text_input(key="handoff_transaction_id").value == "txn_630dea1dda9482dbd765a966355d6708"
    assert [item.value for item in app.text_input][1:] == [
        "120075|81294D7A0", "15126|8127C1990", "2022-09-03 12:27:00",
    ]
    assert app.slider[0].value == 48
    assert app.button[0].label == "Run investigation"
    assert "investigation" not in app.session_state
    app.radio(key="ui_navigation_page").set_value(DEMO).run()
    assert not app.exception
    assert not app.text_input and not app.button
    app.radio(key="ui_navigation_page").set_value(LEGACY).run()
    assert not app.exception
    app.radio[1].set_value("Transaction ID lookup").run()
    assert len(app.text_input) == 1
    assert app.text_input[0].value == "txn_630dea1dda9482dbd765a966355d6708"
    assert "investigation" not in app.session_state
