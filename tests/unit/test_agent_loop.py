import json

import pandas as pd
import pytest

from src import config
from src.agent import loop, prompts, tools
from src.agent.baseline import MODEL_NAME, Settings
from src.agent.data import AgentData
from src.agent.llm import LLMUsage, SpendGuard
from src.agent.loop import LoopSettings, run_agent, single_alert_case
from src.agent.sql import LabelLeakError
from tests.unit.agent_loop_fixtures import (
    DAY,
    ScriptedLLM,
    case_of,
    stub_tools,
    turn,
    valid_payload,
)

SUBMIT = prompts.SUBMIT


def good_run():
    """Graph, then neighbour scores, then a valid raise."""

    return [
        turn(("get_case_graph", {"txn_id": "txn_a"})),
        turn(("get_neighbour_scores", {"txn_ids": ["txn_low", "txn_high"]})),
        turn((SUBMIT, valid_payload())),
    ]


def run(monkeypatch, llm, case=None, **kwargs):
    stub_tools(monkeypatch)
    return run_agent(None, llm, case or case_of("txn_a", "txn_b"), **kwargs)


def tool_names(result, actor):
    return [s["tool"] for s in result["trace"] if s["actor"] == actor]


# --- The main path


def test_a_valid_run_returns_the_standard_result(monkeypatch):
    llm = ScriptedLLM(*good_run())
    result = run(monkeypatch, llm)

    assert result["status"] == "ok"
    assert result["txn_id"] == "txn_a"
    assert result["decision"]["verdict"] == "raise"
    assert result["decision"]["case_id"] == "case_txn_a"
    assert result["stats"] == {
        "model": "scripted-model",
        "steps": 3,
        "tokens_in": 300,
        "tokens_out": 60,
        "latency_ms": result["stats"]["latency_ms"],
        "validation": "valid",
        "repair_used": False,
        "fallback": False,
    }
    assert [c["code"] for c in result["rendered_claims"]] == [
        "FAST_CHAIN",
        "NEIGHBOURS_HIGH_RISK",
    ]
    assert "15 minutes" in result["rendered_claims"][0]["sentence"]
    assert result["graph"]["txn_id"] == "txn_a"
    assert result["neighbours"]["ids"] == ["txn_low", "txn_high"]
    assert result["related"] is None
    json.dumps(result)


def test_the_trace_records_code_and_llm_steps_in_order(monkeypatch):
    result = run(monkeypatch, ScriptedLLM(*good_run()))
    # Code fetches the member the LLM skipped, after the decision.
    assert tool_names(result, "code") == ["get_alert", "get_alert", "get_case_graph"]
    assert tool_names(result, "llm") == [
        "get_case_graph",
        "get_neighbour_scores",
        SUBMIT,
    ]
    assert [s["step"] for s in result["trace"]] == list(range(len(result["trace"])))
    submit = next(s for s in result["trace"] if s["tool"] == SUBMIT)
    assert submit["result"]["valid"] is True


def test_the_model_is_called_at_temperature_zero_with_all_tools(monkeypatch):
    llm = ScriptedLLM(*good_run())
    run(monkeypatch, llm)
    assert {c["temperature"] for c in llm.calls} == {0.0}
    assert [t.name for t in llm.calls[0]["tools"]][-1] == SUBMIT
    assert llm.calls[0]["system"] == prompts.SYSTEM_PROMPT
    first = llm.calls[0]["messages"][0]["content"][0]["text"]
    assert "txn_a" in first and "txn_b" in first and "case_txn_a" in first


def test_tool_results_go_back_as_paired_tool_results_without_the_ids_list(monkeypatch):
    llm = ScriptedLLM(*good_run())
    run(monkeypatch, llm)
    history = llm.calls[1]["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user"]
    block = history[2]["content"][0]["toolResult"]
    assert block["toolUseId"] == history[1]["content"][0]["toolUse"]["toolUseId"]
    assert block["status"] == "success"
    body = json.loads(block["content"][0]["text"])
    assert "ids" not in body and body["no_graph"] is False
    assert "txn_date" not in body["downstream"][0]
    assert "Step 1 of 5 used." in history[2]["content"][1]["text"]


def test_several_tool_calls_in_one_step_run_together(monkeypatch):
    llm = ScriptedLLM(
        turn(
            ("get_case_graph", {"txn_id": "txn_a"}),
            ("get_related_alerts", {"txn_id": "txn_a"}),
        ),
        turn(("get_neighbour_scores", {"txn_ids": ["txn_low"]})),
        turn(
            (
                SUBMIT,
                valid_payload(
                    claims=[{"code": "FAST_CHAIN", "evidence_ids": ["txn_low"]}]
                ),
            )
        ),
    )
    result = run(monkeypatch, llm)
    assert result["stats"]["steps"] == 3
    assert result["related"]["alerts"][0]["txn_id"] == "txn_b"
    assert (
        len(llm.calls[1]["messages"][2]["content"]) == 3
    )  # two results and the step notice


def test_a_member_other_than_the_representative_is_fetched_by_code(monkeypatch):
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_b"})),
        turn((SUBMIT, valid_payload(verdict="raise", claims=[]))),
    )
    result = run(monkeypatch, llm)
    assert tool_names(result, "code") == ["get_alert", "get_alert", "get_case_graph"]
    assert result["graph"] is None  # The LLM never fetched the representative's graph.
    assert [m["graph"]["txn_id"] for m in result["members"]] == ["txn_a", "txn_b"]


def test_every_member_comes_back_with_its_alert_and_graph(monkeypatch):
    llm = ScriptedLLM(*good_run())
    result = run(monkeypatch, llm)
    assert [m["alert"]["txn_id"] for m in result["members"]] == ["txn_a", "txn_b"]
    assert [m["graph"]["txn_id"] for m in result["members"]] == ["txn_a", "txn_b"]


def test_a_graph_fetched_for_the_analyst_never_reaches_the_model(monkeypatch):
    llm = ScriptedLLM(*good_run())
    result = run(monkeypatch, llm)
    assert len(llm.calls) == 3  # No model call after the decision.
    llm_graphs = [s for s in result["trace"] if s["tool"] == "get_case_graph" and s["actor"] == "llm"]
    assert [s["inputs"]["txn_id"] for s in llm_graphs] == ["txn_a"]
    assert all("txn_b" not in str(m) for m in llm.calls[0]["messages"][1:])


def test_a_graph_the_model_fetched_is_not_fetched_again(monkeypatch):
    calls = stub_tools(monkeypatch)
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_a", "window_h": 24})),
        turn((SUBMIT, valid_payload(verdict="not_sure", claims=[]))),
    )
    result = run_agent(None, llm, case_of("txn_a"))
    assert [c for c in calls if c[0] == "get_case_graph"] == [("get_case_graph", "txn_a", 24)]
    assert result["members"][0]["graph"]["window_h"] == 24


def test_the_cutoff_reaches_every_tool_that_reads_later_data(monkeypatch):
    calls = stub_tools(monkeypatch)
    settings = LoopSettings(s1=Settings(as_of="2022-09-14 23:59:59"))
    llm = ScriptedLLM(
        turn(
            ("get_case_graph", {"txn_id": "txn_a"}),
            ("get_related_alerts", {"txn_id": "txn_a"}),
        ),
        turn((SUBMIT, valid_payload(verdict="not_sure", claims=[]))),
    )
    run_agent(None, llm, case_of("txn_a", "txn_b"), settings=settings)
    assert calls.as_of_seen  # Includes the graph code fetched for txn_b.
    assert set(calls.as_of_seen) == {"2022-09-14 23:59:59"}


def test_the_note_and_window_are_passed_through(monkeypatch):
    calls = stub_tools(monkeypatch)
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_a", "window_h": 24})),
        turn(("get_neighbour_scores", {"txn_ids": ["txn_low", "txn_high"]})),
        turn((SUBMIT, valid_payload(note="Chain of 15 minutes."))),
    )
    result = run_agent(None, llm, case_of("txn_a"))
    assert ("get_case_graph", "txn_a", 24) in calls
    assert result["decision"]["note"] == "Chain of 15 minutes."
    assert result["decision"]["case_id"] is None  # A single alert has no case_id.


# --- Tool input checks


def test_unknown_ids_and_bad_inputs_are_returned_to_the_model_as_errors(monkeypatch):
    llm = ScriptedLLM(
        turn(
            ("get_case_graph", {"txn_id": "txn_made_up"}),
            ("get_neighbour_scores", {"txn_ids": ["txn_made_up"]}),
            ("get_case_graph", {"txn_id": "txn_a", "window_h": 500}),
            ("get_nothing", {}),
        ),
        *good_run(),
    )
    result = run(monkeypatch, llm)
    blocks = llm.calls[1]["messages"][2]["content"]
    assert [b["toolResult"]["status"] for b in blocks[:4]] == ["error"] * 4
    assert "not a transaction ID" in blocks[0]["toolResult"]["content"][0]["text"]
    assert "Window must be" in blocks[2]["toolResult"]["content"][0]["text"]
    assert "Unknown tool" in blocks[3]["toolResult"]["content"][0]["text"]
    assert result["stats"]["fallback"] is False


def test_graphs_are_for_case_members_only(monkeypatch):
    llm = ScriptedLLM(
        turn(("get_neighbour_scores", {"txn_ids": ["txn_a"]})),  # a member: allowed
        turn(
            ("get_case_graph", {"txn_id": "txn_low"})
        ),  # known after a graph, but not a member
        turn(
            (SUBMIT, valid_payload(verdict="not_sure", claims=[]))
        ),
    )
    stub_tools(monkeypatch, ranks={"txn_low": 900, "txn_a": 2})
    result = run_agent(None, llm, case_of("txn_a"))
    second = llm.calls[2]["messages"][-1]["content"][0]["toolResult"]
    assert second["status"] == "error"
    assert result["stats"]["fallback"] is False


def test_more_tool_calls_than_allowed_in_one_step_are_refused(monkeypatch):
    calls = [("get_case_graph", {"txn_id": "txn_a"})] * 3
    llm = ScriptedLLM(turn(*calls), *good_run()[1:])
    result = run(monkeypatch, llm, settings=LoopSettings(max_parallel_calls=2))
    blocks = llm.calls[1]["messages"][2]["content"]
    assert [b["toolResult"]["status"] for b in blocks[:3]] == [
        "success",
        "success",
        "error",
    ]
    assert result["stats"]["steps"] == 3


# --- Validation, repair and fallback


def test_an_invalid_decision_gets_one_repair(monkeypatch):
    bad = valid_payload(
        claims=[{"code": "FAST_CHAIN", "evidence_ids": ["txn_invented"]}]
    )
    llm = ScriptedLLM(
        *good_run()[:2], turn((SUBMIT, bad)), turn((SUBMIT, valid_payload()))
    )
    result = run(monkeypatch, llm)

    assert result["stats"]["validation"] == "repaired"
    assert result["stats"]["repair_used"] is True
    assert result["stats"]["fallback"] is False
    assert result["stats"]["steps"] == 3  # The repair is not a step.
    assert llm.calls[3]["force_tool"] == SUBMIT
    rejection = llm.calls[3]["messages"][-1]["content"][0]["toolResult"]
    assert rejection["status"] == "error"
    assert "txn_invented" in rejection["content"][0]["text"]
    assert [s["result"]["valid"] for s in result["trace"] if s["tool"] == SUBMIT] == [
        False,
        True,
    ]


def test_a_decision_still_invalid_after_the_repair_falls_back_to_s1(monkeypatch):
    bad = valid_payload(verdict="arrest")
    llm = ScriptedLLM(*good_run()[:2], turn((SUBMIT, bad)), turn((SUBMIT, bad)))
    result = run(monkeypatch, llm)

    assert result["stats"]["validation"] == "fallback"
    assert result["stats"]["fallback"] is True
    assert result["stats"]["repair_used"] is True
    assert result["stats"]["model"] == "scripted-model"
    assert result["stats"]["tokens_in"] == 400  # The LLM's tokens are kept.
    assert result["decision"]["verdict"] == "raise"  # S1: two members and a top neighbour.
    assert result["decision"]["case_id"] == "case_txn_a"
    assert "fallback" in tool_names(result, "code")
    fallback = next(s for s in result["trace"] if s["tool"] == "fallback")
    assert "invalid after one repair" in fallback["result"]["reason"]
    assert [s["step"] for s in result["trace"]] == list(range(len(result["trace"])))
    assert MODEL_NAME not in json.dumps(result["stats"])


def test_the_last_step_is_forced_to_submit(monkeypatch):
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_a"})),
        turn(("get_related_alerts", {"txn_id": "txn_a"})),
        turn(("get_neighbour_scores", {"txn_ids": ["txn_low", "txn_high"]})),
        turn(("get_alert", {"txn_id": "txn_b"})),
        turn((SUBMIT, valid_payload())),
    )
    result = run(monkeypatch, llm)
    assert [c["force_tool"] for c in llm.calls] == [None, None, None, None, SUBMIT]
    assert result["stats"]["steps"] == 5
    assert (
        "must be submit_decision" in llm.calls[4]["messages"][-1]["content"][-1]["text"]
    )


def test_a_model_that_never_submits_falls_back_after_the_step_limit(monkeypatch):
    llm = ScriptedLLM(*[turn(text="Let me think.") for _ in range(5)])
    result = run(monkeypatch, llm)
    assert len(llm.calls) == 5
    assert result["stats"]["fallback"] is True
    assert result["stats"]["steps"] == 5
    reason = next(s for s in result["trace"] if s["tool"] == "fallback")["result"][
        "reason"
    ]
    assert "step limit" in reason


def test_a_text_only_step_is_nudged(monkeypatch):
    llm = ScriptedLLM(turn(text="Thinking."), *good_run())
    result = run(monkeypatch, llm)
    nudge = llm.calls[1]["messages"][-1]["content"][0]["text"]
    assert nudge.startswith("Call a tool or submit_decision")
    assert result["stats"]["steps"] == 4


def test_a_provider_failure_falls_back(monkeypatch):
    llm = ScriptedLLM(*good_run(), error_at=1)
    result = run(monkeypatch, llm)
    assert result["stats"]["fallback"] is True
    assert result["stats"]["tokens_in"] == 100
    reason = next(s for s in result["trace"] if s["tool"] == "fallback")["result"][
        "reason"
    ]
    assert "throttled" in reason


def test_a_reached_spend_cap_falls_back_without_calling_the_model(monkeypatch):
    spend = SpendGuard(cap_usd=0.0)
    llm = ScriptedLLM()
    result = run(monkeypatch, llm, spend=spend)
    assert llm.calls == []
    assert result["stats"]["fallback"] is True
    assert (
        "spend"
        in next(s for s in result["trace"] if s["tool"] == "fallback")["result"][
            "reason"
        ]
    )


def test_spend_is_recorded_across_a_run(monkeypatch):
    spend = SpendGuard(cap_usd=1e9, price_per_m_tokens=(1_000_000.0, 1_000_000.0))
    run(monkeypatch, ScriptedLLM(*good_run()), spend=spend)
    assert spend.spent_usd == 360.0  # (300 + 60) tokens at $1 each.


def test_the_run_token_cap_falls_back(monkeypatch):
    llm = ScriptedLLM(*good_run())
    result = run(monkeypatch, llm, settings=LoopSettings(run_token_cap=200))
    assert len(llm.calls) == 2
    assert (
        "token cap"
        in next(s for s in result["trace"] if s["tool"] == "fallback")["result"][
            "reason"
        ]
    )


def test_the_run_timeout_falls_back(monkeypatch):
    now = iter(range(0, 10_000, 100))  # The clock jumps 100 s on every read.
    llm = ScriptedLLM(*good_run())
    result = run(
        monkeypatch, llm, clock=lambda: next(now), settings=LoopSettings(timeout_s=150)
    )
    assert result["stats"]["fallback"] is True
    assert (
        "timeout"
        in next(s for s in result["trace"] if s["tool"] == "fallback")["result"][
            "reason"
        ]
    )


def test_a_repair_that_calls_other_tools_falls_back(monkeypatch):
    bad = valid_payload(verdict="arrest")
    llm = ScriptedLLM(
        *good_run()[:2],
        turn((SUBMIT, bad)),
        turn(("get_alert", {"txn_id": "txn_b"})),
    )
    result = run(monkeypatch, llm)
    assert result["stats"]["fallback"] is True


# --- Single alerts and unknown alerts


def test_a_single_alert_case_uses_the_alert_as_the_only_member(monkeypatch):
    case = single_alert_case("txn_a", DAY)
    assert case.member_txn_ids == ("txn_a",) and case.case_id == "case_txn_a"
    stub_tools(monkeypatch)
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_a"})),
        turn((SUBMIT, valid_payload(verdict="raise", claims=[]))),
    )
    result = run_agent(None, llm, case)
    assert result["decision"]["case_id"] is None
    assert "single alert" in llm.calls[0]["messages"][0]["content"][0]["text"]


def test_an_alert_that_is_not_found_returns_not_found_without_calling_the_model(
    monkeypatch,
):
    stub_tools(monkeypatch, found=False)
    llm = ScriptedLLM()
    result = run_agent(None, llm, case_of("txn_a"))
    assert llm.calls == []
    assert result["status"] == "not_found"
    assert result["decision"] is None and result["stats"] is None
    assert result["alert"]["found"] is False


def test_truncation_and_missing_reasons_are_added_and_cap_confidence(monkeypatch):
    stub_tools(monkeypatch, truncated=True, reasons=False)
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_a"})),
        turn(("get_neighbour_scores", {"txn_ids": ["txn_low", "txn_high"]})),
        turn((SUBMIT, valid_payload(confidence="high"))),
    )
    result = run_agent(None, llm, case_of("txn_a"))
    codes = [c["code"] for c in result["decision"]["claims"]]
    assert {"TRUNCATED_WINDOW", "NO_REASONS_AVAILABLE"} <= set(codes)
    assert result["decision"]["confidence"] == "medium"
    assert "runs past the data available" in " ".join(
        c["sentence"] for c in result["rendered_claims"]
    )


# --- Labels never reach the model (PLAN.md rule 2)


def sent_texts(llm):
    for call in llm.calls:
        yield call["system"]
        for message in call["messages"]:
            for block in message["content"]:
                yield json.dumps(block)
        yield json.dumps(
            [[t.name, t.description, t.input_schema] for t in call["tools"]]
        )


def test_nothing_sent_to_the_model_names_a_ground_truth_column(monkeypatch):
    llm = ScriptedLLM(*good_run())
    run(monkeypatch, llm)
    text = " ".join(sent_texts(llm)).lower()
    assert text
    for column in config.AGENT_FORBIDDEN_COLUMNS:
        assert column not in text


@pytest.mark.parametrize(
    "tool", ["get_alert", "get_case_graph", "get_neighbour_scores"]
)
def test_a_label_in_a_tool_result_stops_the_run_instead_of_reaching_the_model(
    monkeypatch, tool
):
    stub_tools(monkeypatch)
    original = getattr(tools, tool)

    def leaky(*args, **kwargs):
        result = original(*args, **kwargs)
        if tool == "get_neighbour_scores":
            result["neighbours"][0]["is_laundering"] = True
        else:
            result["evaluation_is_laundering"] = True
        return result

    monkeypatch.setattr(tools, tool, leaky)
    llm = ScriptedLLM(*good_run())
    with pytest.raises(LabelLeakError):
        run_agent(None, llm, case_of("txn_a"))
    for text in sent_texts(llm):
        for column in config.AGENT_FORBIDDEN_COLUMNS:
            assert column not in text.lower()


def test_a_label_column_in_athena_output_stops_the_run(monkeypatch):
    frame = pd.DataFrame([{"transaction_id": "txn_a", "is_laundering": 1}])
    data = AgentData(lambda sql: (frame, None))
    llm = ScriptedLLM()
    with pytest.raises(LabelLeakError):
        run_agent(data, llm, case_of("txn_a"))
    assert llm.calls == []


def test_the_policy_prompt_and_tool_text_name_no_ground_truth_column():
    everything = prompts.SYSTEM_PROMPT + json.dumps(
        [[t.name, t.description, t.input_schema] for t in prompts.TOOLS]
    )
    for column in config.AGENT_FORBIDDEN_COLUMNS:
        assert column not in everything.lower()


def test_loop_settings_follow_the_config():
    settings = LoopSettings()
    assert settings.max_steps == config.AGENT_MAX_STEPS == 5
    assert settings.s1.window_h == config.AGENT_WINDOW_H_DEFAULT
    assert loop.TEMPERATURE == 0.0
    assert LLMUsage(1, 2) + LLMUsage(3, 4) == LLMUsage(4, 6)
