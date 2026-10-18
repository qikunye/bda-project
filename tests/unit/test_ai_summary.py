from copy import deepcopy

import pytest
from botocore.exceptions import ClientError

from src.ai.llm_client import (
    BedrockConverseClient,
    LLMClientError,
    LLMUsage,
    StructuredCompletion,
)
from src.ai.prompts import ANSWER_TOOL, DEFAULT_SUMMARY_PROMPT, SUMMARY_PROMPTS
from src.ai.summary import generate_case_summary
from src.context.projection import build_llm_input
from src.context.validation import CaseContextIntegrityError
from tests.unit.context_fixtures import make_context

ALERT_ID = "txn_synthetic_alert"
GROUNDED_PAYLOAD = {
    "answer": f"Alert {ALERT_ID} was a WIRE payment of USD 100.25, ranked 12 by BQ1.",
    "evidence": [{"type": "transaction", "id": ALERT_ID}],
    "limitations": [
        {"code": "missing_amounts", "detail": "One amount is missing."},
        {"code": "invalid_amounts", "detail": "One amount is invalid."},
    ],
    "needs_more_context": False,
}
INVENTED_NUMBER_PAYLOAD = {
    **deepcopy(GROUNDED_PAYLOAD),
    "answer": f"Alert {ALERT_ID} was a WIRE payment of USD 999.99.",
}


class ScriptedClient:
    model_id = "scripted-model"

    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return StructuredCompletion(
            payload=self._payloads.pop(0),
            usage=LLMUsage(input_tokens=1000, output_tokens=100),
            latency_ms=500,
            model_id=self.model_id,
            stop_reason="tool_use",
        )


@pytest.fixture
def llm_input():
    return build_llm_input(make_context(), max_candidates=20, max_transactions=100)


def test_grounded_first_answer_is_returned_without_retry(llm_input):
    client = ScriptedClient(GROUNDED_PAYLOAD)

    result = generate_case_summary(llm_input, client)

    assert result.status == "grounded"
    assert result.response.answer == GROUNDED_PAYLOAD["answer"]
    assert len(client.calls) == 1
    assert result.usage == LLMUsage(input_tokens=1000, output_tokens=100)
    assert result.model_id == "scripted-model"
    assert result.prompt_version == "summary-v2"
    assert len(result.context_sha256) == 64


def test_request_carries_rules_context_and_required_codes(llm_input):
    client = ScriptedClient(GROUNDED_PAYLOAD)

    generate_case_summary(llm_input, client)

    call = client.calls[0]
    assert call["system"] == DEFAULT_SUMMARY_PROMPT.system
    assert DEFAULT_SUMMARY_PROMPT.task in call["user_message"]
    assert call["tool"] == ANSWER_TOOL
    assert call["temperature"] == 0.0
    assert "<CASE_CONTEXT>" in call["user_message"]
    assert ALERT_ID in call["user_message"]
    assert (
        'Required limitation codes: ["invalid_amounts", "missing_amounts"]'
        in call["user_message"]
    )


def test_earlier_prompt_version_can_be_selected(llm_input):
    client = ScriptedClient(GROUNDED_PAYLOAD)

    result = generate_case_summary(
        llm_input,
        client,
        prompt=SUMMARY_PROMPTS["summary-v1"],
    )

    assert result.prompt_version == "summary-v1"
    assert client.calls[0]["system"] == SUMMARY_PROMPTS["summary-v1"].system


def test_rejected_answer_is_retried_with_its_problems(llm_input):
    client = ScriptedClient(INVENTED_NUMBER_PAYLOAD, GROUNDED_PAYLOAD)

    result = generate_case_summary(llm_input, client)

    assert result.status == "grounded"
    assert len(result.attempts) == 2
    assert [issue.code for issue in result.attempts[0].issues] == [
        "unsupported_number"
    ]
    retry_message = client.calls[1]["user_message"]
    assert "Your previous answer was rejected" in retry_message
    assert "999.99" in retry_message
    assert result.usage == LLMUsage(input_tokens=2000, output_tokens=200)


def test_answer_failing_every_attempt_is_not_shown(llm_input):
    client = ScriptedClient(INVENTED_NUMBER_PAYLOAD, INVENTED_NUMBER_PAYLOAD)

    result = generate_case_summary(llm_input, client)

    assert result.status == "rejected"
    assert result.response is None
    assert len(result.attempts) == 2


def test_missing_tool_call_and_schema_errors_count_as_failed_attempts(llm_input):
    client = ScriptedClient(None, {"answer": "no other fields"})

    result = generate_case_summary(llm_input, client)

    assert result.status == "rejected"
    assert [attempt.issues[0].code for attempt in result.attempts] == [
        "no_structured_output",
        "schema_error",
    ]


def test_full_context_is_rejected_before_any_model_call():
    client = ScriptedClient(GROUNDED_PAYLOAD)

    with pytest.raises(CaseContextIntegrityError):
        generate_case_summary(make_context(), client)
    assert client.calls == []


def test_invalid_attempt_limit_is_rejected(llm_input):
    with pytest.raises(ValueError):
        generate_case_summary(llm_input, ScriptedClient(), max_attempts=0)


class StubBedrockRuntime:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def converse(self, **request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def bedrock_response(content):
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 1168, "outputTokens": 125, "totalTokens": 1293},
        "metrics": {"latencyMs": 1540},
    }


def complete(client):
    return client.complete_structured(
        system="rules",
        user_message="question",
        tool=ANSWER_TOOL,
        max_tokens=800,
        temperature=0.0,
    )


def test_bedrock_client_forces_the_answer_tool():
    runtime = StubBedrockRuntime(
        bedrock_response(
            [{"toolUse": {"toolUseId": "t1", "name": "submit_answer", "input": GROUNDED_PAYLOAD}}]
        )
    )

    completion = complete(BedrockConverseClient(runtime, model_id="model-x"))

    request = runtime.requests[0]
    assert request["modelId"] == "model-x"
    assert request["system"] == [{"text": "rules"}]
    assert request["messages"] == [
        {"role": "user", "content": [{"text": "question"}]}
    ]
    assert request["toolConfig"]["toolChoice"] == {
        "tool": {"name": "submit_answer"}
    }
    assert request["inferenceConfig"] == {"maxTokens": 800, "temperature": 0.0}
    assert completion.payload == GROUNDED_PAYLOAD
    assert completion.usage == LLMUsage(input_tokens=1168, output_tokens=125)
    assert completion.latency_ms == 1540


def test_bedrock_client_reports_missing_tool_call_as_no_payload():
    runtime = StubBedrockRuntime(bedrock_response([{"text": "plain text"}]))

    completion = complete(BedrockConverseClient(runtime))

    assert completion.payload is None
    assert completion.usage.input_tokens == 1168


def test_bedrock_provider_failure_raises_client_error():
    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not available"}},
        "Converse",
    )

    with pytest.raises(LLMClientError, match="AccessDeniedException"):
        complete(BedrockConverseClient(StubBedrockRuntime(error=denied)))
