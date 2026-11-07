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
    """Graph, then neighbour scores, then a valid brief."""

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
    assert result["case_id"] == "case_txn_a"
    assert result["fallback"] is False
    assert result["summary"] == "Money came in and went on quickly."
    assert result["stats"] == {
        "model": "scripted-model",
        "steps": 3,
        "tokens_in": 300,
        "tokens_out": 60,
        "latency_ms": result["stats"]["latency_ms"],
        "validation": "valid",
        "repair_used": False,
        "fallback": False,
        "observations_dropped": 0,
    }
    assert [f["code"] for f in result["findings"]] == [
        "PASS_THROUGH",
        "NEIGHBOURS_HIGH_RISK",
    ]
    assert "15 minutes" in result["findings"][0]["sentence"]
    assert result["findings"][0]["evidence_ids"] == ["txn_high", "txn_up"]
    assert [c["code"] for c in result["next_checks"]] == ["CHECK_ONWARD_ACCOUNTS"]
    # The model never fetched account activity, so code says the history was not checked.
    assert [g["code"] for g in result["gaps"]] == ["SHORT_HISTORY"]
    assert [m["txn_id"] for m in result["money_flow"]] == ["txn_a", "txn_b"]
    assert result["money_flow"][0]["per_currency"]["US Dollar"]["quick_onward_share"] == 0.8
    assert "decision" not in result and "rendered_claims" not in result
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
    assert f"Step 1 of {config.AGENT_MAX_STEPS} used." in history[2]["content"][1]["text"]


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
                    findings=[
                        {"code": "REPEAT_ACCOUNT_ALERTS", "evidence_ids": ["txn_ext"]}
                    ],
                    next_checks=["REVIEW_RELATED_ALERTS"],
                ),
            )
        ),
    )
    result = run(monkeypatch, llm)
    assert result["stats"]["steps"] == 3
    assert [f["code"] for f in result["findings"]] == ["REPEAT_ACCOUNT_ALERTS"]
    assert (
        len(llm.calls[1]["messages"][2]["content"]) == 3
    )  # two results and the step notice


def test_a_member_other_than_the_representative_is_fetched_by_code(monkeypatch):
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_b"})),
        turn((SUBMIT, valid_payload(findings=[], next_checks=[]))),
    )
    result = run(monkeypatch, llm)
    assert tool_names(result, "code") == ["get_alert", "get_alert", "get_case_graph"]
    assert result["findings"] == []  # An empty brief is a valid answer.
    assert [m["graph"]["txn_id"] for m in result["members"]] == ["txn_a", "txn_b"]
    assert [m["txn_id"] for m in result["money_flow"]] == ["txn_a", "txn_b"]


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
        turn((SUBMIT, valid_payload(findings=[], next_checks=[]))),
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
            ("get_account_activity", {"txn_id": "txn_a", "side": "sender"}),
            ("get_related_alerts", {"txn_id": "txn_a"}),
        ),
        turn((SUBMIT, valid_payload(findings=[], next_checks=[]))),
    )
    run_agent(None, llm, case_of("txn_a", "txn_b"), settings=settings)
    assert len(calls.as_of_seen) >= 4  # Includes the graph code fetched for txn_b.
    assert set(calls.as_of_seen) == {"2022-09-14 23:59:59"}


def test_the_summary_and_window_are_passed_through(monkeypatch):
    calls = stub_tools(monkeypatch)
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_a", "window_h": 24})),
        turn(("get_neighbour_scores", {"txn_ids": ["txn_low", "txn_high"]})),
        turn((SUBMIT, valid_payload(summary="Money moves on quickly."))),
    )
    result = run_agent(None, llm, case_of("txn_a"))
    assert ("get_case_graph", "txn_a", 24) in calls
    assert result["summary"] == "Money moves on quickly."
    assert result["case_id"] is None  # A single alert has no case_id.


# --- Tool input checks


def test_unknown_ids_and_bad_inputs_are_returned_to_the_model_as_errors(monkeypatch):
    llm = ScriptedLLM(
        turn(
            ("get_case_graph", {"txn_id": "txn_made_up"}),
            ("get_neighbour_scores", {"txn_ids": ["txn_made_up"]}),
            ("get_case_graph", {"txn_id": "txn_a", "window_h": 500}),
            ("get_nothing", {}),
        ),
        turn(
            ("get_account_activity", {"txn_id": "txn_a", "side": "both"}),
            ("get_related_alerts", {"txn_id": "txn_a", "accounts": ["99|NOPE"]}),
            ("get_related_alerts", {"txn_id": "txn_a", "accounts": "3|C"}),
        ),
        *good_run(),
    )
    result = run(monkeypatch, llm)
    blocks = llm.calls[1]["messages"][2]["content"]
    assert [b["toolResult"]["status"] for b in blocks[:4]] == ["error"] * 4
    assert "not a transaction ID" in blocks[0]["toolResult"]["content"][0]["text"]
    assert "Window must be" in blocks[2]["toolResult"]["content"][0]["text"]
    assert "Unknown tool" in blocks[3]["toolResult"]["content"][0]["text"]
    blocks = llm.calls[2]["messages"][-1]["content"]
    assert [b["toolResult"]["status"] for b in blocks[:3]] == ["error"] * 3
    assert "side must be" in blocks[0]["toolResult"]["content"][0]["text"]
    assert "not an account ID" in blocks[1]["toolResult"]["content"][0]["text"]
    assert "list of account ID strings" in blocks[2]["toolResult"]["content"][0]["text"]
    assert result["stats"]["fallback"] is False


def test_graphs_are_for_case_members_only(monkeypatch):
    llm = ScriptedLLM(
        turn(("get_neighbour_scores", {"txn_ids": ["txn_a"]})),  # a member: allowed
        turn(
            ("get_case_graph", {"txn_id": "txn_low"})
        ),  # known after a graph, but not a member
        turn((SUBMIT, valid_payload(findings=[], next_checks=[]))),
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


def test_an_invalid_brief_gets_one_repair(monkeypatch):
    bad = valid_payload(
        findings=[{"code": "PASS_THROUGH", "evidence_ids": ["txn_invented"]}]
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


def test_a_brief_still_invalid_after_the_repair_falls_back_to_s1(monkeypatch):
    bad = valid_payload(summary="It moved 5 times.")
    llm = ScriptedLLM(*good_run()[:2], turn((SUBMIT, bad)), turn((SUBMIT, bad)))
    result = run(monkeypatch, llm)

    assert result["stats"]["validation"] == "fallback"
    assert result["stats"]["fallback"] is True
    assert result["stats"]["repair_used"] is True
    assert result["stats"]["model"] == "scripted-model"
    assert result["stats"]["tokens_in"] == 400  # The LLM's tokens are kept.
    assert result["fallback"] is True
    assert "PASS_THROUGH" in [f["code"] for f in result["findings"]]  # The code-only brief's rules found it.
    assert result["case_id"] == "case_txn_a"
    assert "fixed rules" in result["summary"]  # The code-only template, not the model's words.
    assert "fallback" in tool_names(result, "code")
    fallback = next(s for s in result["trace"] if s["tool"] == "fallback")
    assert "still invalid after one repair" in fallback["result"]["reason"]
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
    result = run(monkeypatch, llm, settings=LoopSettings(max_steps=5))
    assert [c["force_tool"] for c in llm.calls] == [None, None, None, None, SUBMIT]
    assert result["stats"]["steps"] == 5
    assert "must be submit_brief" in llm.calls[4]["messages"][-1]["content"][-1]["text"]


def test_a_model_that_never_submits_falls_back_after_the_step_limit(monkeypatch):
    llm = ScriptedLLM(*[turn(text="Let me think.") for _ in range(5)])
    result = run(monkeypatch, llm, settings=LoopSettings(max_steps=5))
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
    assert nudge.startswith("Call a tool or submit_brief")
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
    bad = valid_payload(summary="It moved 5 times.")
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
        turn((SUBMIT, valid_payload(findings=[], next_checks=[]))),
    )
    result = run_agent(None, llm, case)
    assert result["case_id"] is None
    assert "single alert" in llm.calls[0]["messages"][0]["content"][0]["text"]


def test_an_alert_that_is_not_found_returns_not_found_without_calling_the_model(
    monkeypatch,
):
    stub_tools(monkeypatch, found=False)
    llm = ScriptedLLM()
    result = run_agent(None, llm, case_of("txn_a"))
    assert llm.calls == []
    assert result["status"] == "not_found"
    assert result["summary"] is None and result["stats"] is None
    assert result["findings"] == [] and result["money_flow"] == []
    assert result["alert"]["found"] is False


def test_gaps_are_set_by_code_from_the_tool_flags(monkeypatch):
    monkeypatch.setattr(config, "AGENT_DATA_HORIZON", "2022-09-15 00:00:00")
    stub_tools(monkeypatch, truncated=True)
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_a"})),
        turn(("get_neighbour_scores", {"txn_ids": ["txn_low", "txn_high"]})),
        # No gap code in the brief: code adds them, and they allow RECHECK_AFTER_MORE_DATA.
        turn((SUBMIT, valid_payload(next_checks=["RECHECK_AFTER_MORE_DATA"]))),
    )
    result = run_agent(None, llm, case_of("txn_a"))
    assert [g["code"] for g in result["gaps"]] == [
        "TRUNCATED_WINDOW",
        "SHORT_OBSERVATION",
        "SHORT_HISTORY",
    ]
    assert "runs past the end of the data" in result["gaps"][0]["sentence"]
    assert [c["code"] for c in result["next_checks"]] == ["RECHECK_AFTER_MORE_DATA"]
    assert result["money_flow"][0]["hours_observed_after_alert"] == 2.0


def test_a_brief_that_writes_its_own_gaps_or_money_flow_is_rejected(monkeypatch):
    bad = valid_payload(gaps=["TRUNCATED_WINDOW"], money_flow=[])
    llm = ScriptedLLM(*good_run()[:2], turn((SUBMIT, bad)), turn((SUBMIT, valid_payload())))
    result = run(monkeypatch, llm)
    assert result["stats"]["validation"] == "repaired"
    rejection = llm.calls[3]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"]
    assert "set by code" in rejection


# --- The activity tool and extra accounts


def test_related_alerts_never_count_the_cases_own_members_as_repeats(monkeypatch):
    calls = stub_tools(monkeypatch)
    llm = ScriptedLLM(
        turn(("get_related_alerts", {"txn_id": "txn_b"})),
        turn(
            (
                SUBMIT,
                valid_payload(
                    findings=[{"code": "REPEAT_ACCOUNT_ALERTS", "evidence_ids": ["txn_a"]}],
                    next_checks=[],
                ),
            )
        ),
        turn(
            (
                SUBMIT,
                valid_payload(
                    findings=[{"code": "REPEAT_ACCOUNT_ALERTS", "evidence_ids": ["txn_ext"]}],
                    next_checks=["REVIEW_RELATED_ALERTS"],
                ),
            )
        ),
    )
    result = run_agent(None, llm, case_of("txn_a", "txn_b"))
    related = next(c for c in calls if c[0] == "get_related_alerts")
    assert related[2] == ("txn_a", "txn_b")  # Every member, not only the one searched.
    assert result["stats"]["validation"] == "repaired"  # Citing a member was refused.
    assert [f["evidence_ids"] for f in result["findings"]] == [["txn_ext"]]


def test_account_activity_supports_a_fan_out_finding(monkeypatch):
    stub_tools(monkeypatch, fan=6)
    llm = ScriptedLLM(
        turn(("get_account_activity", {"txn_id": "txn_a", "side": "sender"})),
        turn(
            (
                SUBMIT,
                valid_payload(
                    findings=[{"code": "FAN_OUT", "evidence_ids": ["txn_so1", "txn_so2"]}],
                    next_checks=["CHECK_ONWARD_ACCOUNTS"],
                ),
            )
        ),
    )
    result = run_agent(None, llm, case_of("txn_a"))
    assert [f["code"] for f in result["findings"]] == ["FAN_OUT"]
    assert "6 different accounts" in result["findings"][0]["sentence"]
    assert result["stats"]["fallback"] is False
    assert "get_account_activity" in tool_names(result, "llm")


def test_a_fan_out_finding_without_enough_counterparties_is_rejected(monkeypatch):
    stub_tools(monkeypatch, fan=2)
    bad = valid_payload(findings=[{"code": "FAN_OUT", "evidence_ids": ["txn_so1"]}])
    llm = ScriptedLLM(
        turn(("get_account_activity", {"txn_id": "txn_a", "side": "sender"})),
        turn((SUBMIT, bad)),
        turn((SUBMIT, valid_payload(findings=[], next_checks=[]))),
    )
    result = run_agent(None, llm, case_of("txn_a"))
    assert result["stats"]["validation"] == "repaired"
    rejection = llm.calls[2]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"]
    assert "not supported by the tool results" in rejection


def test_related_alerts_can_include_accounts_a_tool_returned(monkeypatch):
    calls = stub_tools(monkeypatch)
    llm = ScriptedLLM(
        turn(("get_case_graph", {"txn_id": "txn_a"})),
        turn(("get_related_alerts", {"txn_id": "txn_a", "accounts": ["3|C"]})),
        turn((SUBMIT, valid_payload(findings=[], next_checks=[]))),
    )
    run_agent(None, llm, case_of("txn_a"))
    related = next(c for c in calls if c[0] == "get_related_alerts")
    assert related[1] == ("1|A", "2|B", "3|C")  # The member's own accounts, then the extra.


# --- Labels never reach the model


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
    "tool",
    ["get_alert", "get_case_graph", "get_neighbour_scores", "get_account_activity"],
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
    llm = ScriptedLLM(
        turn(("get_account_activity", {"txn_id": "txn_a", "side": "sender"})),
        *good_run(),
    )
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
    assert settings.max_steps == config.AGENT_MAX_STEPS == 8
    assert settings.s1.window_h == config.AGENT_WINDOW_H_DEFAULT
    assert loop.TEMPERATURE == 0.0
    assert LLMUsage(1, 2) + LLMUsage(3, 4) == LLMUsage(4, 6)


# --- Observations


def test_observations_come_back_with_the_brief_and_are_not_changed(monkeypatch):
    remark = {
        "text": "The onward payments appear close together.",
        "evidence_ids": ["txn_low", "txn_high"],
    }
    llm = ScriptedLLM(
        *good_run()[:2], turn((SUBMIT, valid_payload(observations=[remark])))
    )
    result = run(monkeypatch, llm)
    assert result["observations"] == [remark]
    assert [f["code"] for f in result["findings"]] == ["PASS_THROUGH", "NEIGHBOURS_HIGH_RISK"]
    assert result["fallback"] is False


def test_a_bad_observation_is_dropped_without_a_repair_and_logged(monkeypatch):
    good = {"text": "The onward payments appear quick.", "evidence_ids": ["txn_low"]}
    payload = valid_payload(
        observations=[
            {"text": "This is money laundering.", "evidence_ids": ["txn_low"]},
            good,
            {"text": "Money appears to go back to the sender.", "evidence_ids": ["txn_low"]},
        ]
    )
    llm = ScriptedLLM(*good_run()[:2], turn((SUBMIT, payload)))
    result = run(monkeypatch, llm)
    assert result["fallback"] is False
    assert result["stats"]["validation"] == "valid"
    assert result["stats"]["repair_used"] is False
    assert result["stats"]["observations_dropped"] == 2
    assert result["observations"] == [good]
    assert [f["code"] for f in result["findings"]] == ["PASS_THROUGH", "NEIGHBOURS_HIGH_RISK"]
    submit_step = next(s for s in result["trace"] if s["tool"] == SUBMIT)
    dropped = " ".join(submit_step["result"]["dropped_observations"])
    assert "not accuse" in dropped and "describes MONEY_RETURNS" in dropped


def test_a_fallback_brief_has_no_observations(monkeypatch):
    remark = {"text": "Something appears odd.", "evidence_ids": ["txn_low"]}
    bad = valid_payload(summary="It moved 5 times.", observations=[remark])
    llm = ScriptedLLM(*good_run()[:2], turn((SUBMIT, bad)), turn((SUBMIT, bad)))
    result = run(monkeypatch, llm)
    assert result["fallback"] is True
    assert result["observations"] == []  # Only the LLM writes observations.


def test_the_prompt_and_submit_tool_offer_observations():
    assert "observations" in prompts.SYSTEM_PROMPT
    submit = next(t for t in prompts.TOOLS if t.name == SUBMIT)
    schema = submit.input_schema
    assert "observations" in schema["properties"]
    assert "observations" not in schema["required"]
    assert schema["properties"]["observations"]["maxItems"] == config.AGENT_MAX_OBSERVATIONS
