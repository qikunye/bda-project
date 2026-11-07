import pytest

from src.agent import investigate as investigate_module
from src.agent.investigate import investigate
from tests.unit.agent_loop_fixtures import (
    DAY,
    ScriptedLLM,
    stub_tools,
    turn,
    valid_payload,
)

SUBMIT = "submit_brief"


def test_it_investigates_one_alert_with_the_window_given(monkeypatch):
    calls = stub_tools(monkeypatch)
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_a"})),
        turn(("get_neighbour_scores", {"txn_ids": ["txn_low", "txn_high"]})),
        turn((SUBMIT, valid_payload())),
    )
    result = investigate("txn_a", DAY, 24, data=object(), llm=llm)

    assert result["status"] == "ok" and result["window_h"] == 24
    assert [f["code"] for f in result["findings"]] == ["PASS_THROUGH", "NEIGHBOURS_HIGH_RISK"]
    assert result["case_id"] is None
    assert ("get_case_graph", "txn_a", 24) in calls
    assert "single alert" in llm.calls[0]["messages"][0]["content"][0]["text"]


def test_the_default_window_is_48_hours(monkeypatch):
    stub_tools(monkeypatch)
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_a"})),
        turn((SUBMIT, valid_payload(findings=[], next_checks=[]))),
    )
    assert investigate("txn_a", DAY, data=object(), llm=llm)["window_h"] == 48


@pytest.mark.parametrize("window", [5, 73, 48.0, "48", None])
def test_a_window_outside_6_to_72_is_an_error(window):
    with pytest.raises(ValueError, match="Window must be"):
        investigate("txn_a", DAY, window, data=object(), llm=ScriptedLLM())


@pytest.mark.parametrize(
    "txn_id, txn_date",
    [("not-an-id", DAY), ("txn_a", "14/09/2022"), ("", ""), (None, None)],
)
def test_a_malformed_id_or_date_is_not_found_without_calling_anything(txn_id, txn_date):
    llm = ScriptedLLM()
    result = investigate(txn_id, txn_date, data=object(), llm=llm)
    assert result["status"] == "not_found"
    assert (
        result["summary"] is None and result["stats"] is None and result["trace"] == []
    )
    assert result["alert"]["found"] is False
    assert result["findings"] == [] and result["money_flow"] == [] and result["case_id"] is None
    assert llm.calls == []


def test_an_unknown_alert_is_not_found(monkeypatch):
    stub_tools(monkeypatch, found=False)
    llm = ScriptedLLM()
    result = investigate("txn_a", DAY, data=object(), llm=llm)
    assert result["status"] == "not_found" and llm.calls == []


def test_the_app_shares_one_data_source_model_client_and_spend_cap(monkeypatch):
    created = []
    monkeypatch.setattr(
        investigate_module,
        "athena_runner",
        lambda: created.append("athena") or (lambda sql: None),
    )
    monkeypatch.setattr(
        investigate_module,
        "create_agent_llm",
        lambda: created.append("llm") or ScriptedLLM(),
    )
    monkeypatch.setattr(investigate_module, "_shared", {})
    first = investigate_module._shared_resources()
    second = investigate_module._shared_resources()
    assert first == second and created == ["athena", "llm"]
