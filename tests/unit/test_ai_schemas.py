from copy import deepcopy

import pytest

from src.ai.schemas import (
    RESPONSE_JSON_SCHEMA,
    CopilotResponseSchemaError,
    EvidenceReference,
    Limitation,
    parse_copilot_response,
)

VALID_PAYLOAD = {
    "answer": "The alert txn_synthetic_alert was a WIRE payment.",
    "evidence": [{"type": "transaction", "id": "txn_synthetic_alert"}],
    "limitations": [
        {"code": "missing_amounts", "detail": "One amount is missing."}
    ],
    "needs_more_context": False,
}


def test_valid_payload_parses_to_typed_response():
    response = parse_copilot_response(VALID_PAYLOAD)

    assert response.answer == VALID_PAYLOAD["answer"]
    assert response.evidence == (
        EvidenceReference(type="transaction", id="txn_synthetic_alert"),
    )
    assert response.limitations == (
        Limitation(code="missing_amounts", detail="One amount is missing."),
    )
    assert response.needs_more_context is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("limitations"),
        lambda payload: payload.update(extra="field"),
        lambda payload: payload.update(answer="   "),
        lambda payload: payload.update(answer="x" * 4001),
        lambda payload: payload.update(evidence="txn_synthetic_alert"),
        lambda payload: payload["evidence"][0].update(type="entity"),
        lambda payload: payload["evidence"][0].pop("id"),
        lambda payload: payload["limitations"][0].update(code="unknown"),
        lambda payload: payload.update(needs_more_context="false"),
    ],
)
def test_malformed_payload_is_rejected(mutate):
    payload = deepcopy(VALID_PAYLOAD)
    mutate(payload)

    with pytest.raises(CopilotResponseSchemaError):
        parse_copilot_response(payload)


def test_non_object_payload_is_rejected():
    with pytest.raises(CopilotResponseSchemaError):
        parse_copilot_response('{"answer": "text"}')


def test_json_schema_matches_parser_contract():
    assert set(RESPONSE_JSON_SCHEMA["required"]) == set(VALID_PAYLOAD)
    assert RESPONSE_JSON_SCHEMA["additionalProperties"] is False
