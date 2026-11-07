import pytest
from botocore.exceptions import ClientError

from src.agent import prompts
from src.agent.llm import (
    BedrockAgentLLM,
    LLMClientError,
    LLMUsage,
    SpendCapReached,
    SpendGuard,
)


class FakeBedrock:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def converse(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


def reply(content, **extra):
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "usage": {"inputTokens": 120, "outputTokens": 30},
        "metrics": {"latencyMs": 800},
        "stopReason": "tool_use",
        **extra,
    }


def call(client, force_tool=None):
    return BedrockAgentLLM(client, model_id="m").converse(
        system="policy",
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        tools=prompts.TOOLS,
        force_tool=force_tool,
        max_tokens=100,
        temperature=0.0,
    )


def test_tool_calls_text_and_usage_are_parsed():
    client = FakeBedrock(
        reply(
            [
                {"text": "Checking."},
                {
                    "toolUse": {
                        "toolUseId": "u1",
                        "name": "get_case_graph",
                        "input": {"txn_id": "txn_a"},
                    }
                },
                {
                    "toolUse": {
                        "toolUseId": "u2",
                        "name": "get_related_alerts",
                        "input": {"txn_id": "txn_a"},
                    }
                },
            ]
        )
    )
    turn = call(client)
    assert turn.text == "Checking."
    assert [(c.id, c.name, c.input) for c in turn.tool_calls] == [
        ("u1", "get_case_graph", {"txn_id": "txn_a"}),
        ("u2", "get_related_alerts", {"txn_id": "txn_a"}),
    ]
    assert turn.usage == LLMUsage(120, 30)
    assert turn.latency_ms == 800
    assert turn.stop_reason == "tool_use"
    assert turn.content[1]["toolUse"]["toolUseId"] == "u1"


def test_a_reply_without_tool_calls_has_none():
    turn = call(FakeBedrock(reply([{"text": "No."}], stopReason="end_turn")))
    assert turn.tool_calls == () and turn.text == "No."


def test_tool_input_that_is_not_an_object_becomes_empty():
    client = FakeBedrock(
        reply([{"toolUse": {"toolUseId": "u", "name": "get_alert", "input": "x"}}])
    )
    assert call(client).tool_calls[0].input == {}


def test_the_request_sends_every_tool_and_lets_the_model_choose_by_default():
    client = FakeBedrock(reply([{"text": "ok"}]))
    call(client)
    request = client.requests[0]
    assert request["modelId"] == "m"
    assert request["system"] == [{"text": "policy"}]
    assert request["toolConfig"]["toolChoice"] == {"auto": {}}
    assert [t["toolSpec"]["name"] for t in request["toolConfig"]["tools"]] == [
        t.name for t in prompts.TOOLS
    ]
    assert request["inferenceConfig"] == {"maxTokens": 100, "temperature": 0.0}


def test_a_forced_tool_is_requested_by_name():
    client = FakeBedrock(reply([{"text": "ok"}]))
    call(client, force_tool="submit_decision")
    assert client.requests[0]["toolConfig"]["toolChoice"] == {
        "tool": {"name": "submit_decision"}
    }


def test_provider_errors_become_llm_client_errors():
    error = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse"
    )
    with pytest.raises(LLMClientError, match="ThrottlingException"):
        call(FakeBedrock(error=error))


def test_a_missing_latency_is_measured():
    response = reply([{"text": "ok"}])
    del response["metrics"]
    assert call(FakeBedrock(response)).latency_ms >= 0


# --- Spend guard


def test_spend_guard_adds_up_cost_and_stops_at_the_cap():
    guard = SpendGuard(cap_usd=1.0, price_per_m_tokens=(3.0, 15.0))
    guard.check()
    guard.record(LLMUsage(100_000, 10_000))  # 0.30 + 0.15
    assert guard.spent_usd == pytest.approx(0.45)
    guard.check()
    guard.record(LLMUsage(200_000, 20_000))  # 0.60 + 0.30
    with pytest.raises(SpendCapReached):
        guard.check()


def test_spend_guard_counts_every_thread():
    from concurrent.futures import ThreadPoolExecutor

    guard = SpendGuard(cap_usd=100.0, price_per_m_tokens=(1_000_000.0, 0.0))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: guard.record(LLMUsage(1, 0)), range(400)))
    assert guard.spent_usd == 400.0
