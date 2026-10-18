"""Structured response contract for AI Copilot answers."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

EVIDENCE_TYPES = ("transaction", "relationship", "account")

# Disclosure codes the model may attach to an answer. Every code except
# "other" corresponds to a condition that can be derived from the bounded
# CaseContext, so the grounding check can verify it deterministically.
DISCLOSURE_CODES = (
    "evidence_truncated",
    "missing_amounts",
    "invalid_amounts",
    "amounts_without_currency",
    "bq1_not_available",
    "model_explanations_unavailable",
    "wider_window_not_queried",
    "other",
)

MAX_ANSWER_CHARACTERS = 4000
_RESPONSE_KEYS = {"answer", "evidence", "limitations", "needs_more_context"}

# JSON Schema for the same contract, for use as a forced tool input schema.
RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(_RESPONSE_KEYS),
    "properties": {
        "answer": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_ANSWER_CHARACTERS,
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "id"],
                "properties": {
                    "type": {"type": "string", "enum": list(EVIDENCE_TYPES)},
                    "id": {"type": "string", "minLength": 1},
                },
            },
        },
        "limitations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "detail"],
                "properties": {
                    "code": {"type": "string", "enum": list(DISCLOSURE_CODES)},
                    "detail": {"type": "string", "minLength": 1},
                },
            },
        },
        "needs_more_context": {"type": "boolean"},
    },
}


class CopilotResponseSchemaError(ValueError):
    """Raised when a model response does not match the response contract."""


@dataclass(frozen=True)
class EvidenceReference:
    type: str
    id: str


@dataclass(frozen=True)
class Limitation:
    code: str
    detail: str


@dataclass(frozen=True)
class CopilotResponse:
    answer: str
    evidence: tuple[EvidenceReference, ...]
    limitations: tuple[Limitation, ...]
    needs_more_context: bool


def _require_object(
    value: Any,
    field_name: str,
    keys: set[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CopilotResponseSchemaError(f"{field_name} must be an object.")
    if set(value) != keys:
        raise CopilotResponseSchemaError(
            f"{field_name} must have exactly the keys {sorted(keys)}."
        )
    return value


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CopilotResponseSchemaError(
            f"{field_name} must be a non-empty string."
        )
    return value


def _require_list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise CopilotResponseSchemaError(f"{field_name} must be a list.")
    return value


def parse_copilot_response(payload: Any) -> CopilotResponse:
    """Parse a model's structured output into a typed CopilotResponse."""

    response = _require_object(payload, "response", _RESPONSE_KEYS)

    answer = _require_text(response["answer"], "answer")
    if len(answer) > MAX_ANSWER_CHARACTERS:
        raise CopilotResponseSchemaError(
            f"answer must be at most {MAX_ANSWER_CHARACTERS} characters."
        )

    evidence = []
    for index, item in enumerate(
        _require_list(response["evidence"], "evidence")
    ):
        field_name = f"evidence[{index}]"
        record = _require_object(item, field_name, {"type", "id"})
        evidence_type = _require_text(record["type"], f"{field_name}.type")
        if evidence_type not in EVIDENCE_TYPES:
            raise CopilotResponseSchemaError(
                f"{field_name}.type must be one of {list(EVIDENCE_TYPES)}."
            )
        evidence.append(
            EvidenceReference(
                type=evidence_type,
                id=_require_text(record["id"], f"{field_name}.id"),
            )
        )

    limitations = []
    for index, item in enumerate(
        _require_list(response["limitations"], "limitations")
    ):
        field_name = f"limitations[{index}]"
        record = _require_object(item, field_name, {"code", "detail"})
        code = _require_text(record["code"], f"{field_name}.code")
        if code not in DISCLOSURE_CODES:
            raise CopilotResponseSchemaError(
                f"{field_name}.code must be one of {list(DISCLOSURE_CODES)}."
            )
        limitations.append(
            Limitation(
                code=code,
                detail=_require_text(record["detail"], f"{field_name}.detail"),
            )
        )

    needs_more_context = response["needs_more_context"]
    if not isinstance(needs_more_context, bool):
        raise CopilotResponseSchemaError(
            "needs_more_context must be a boolean."
        )

    return CopilotResponse(
        answer=answer,
        evidence=tuple(evidence),
        limitations=tuple(limitations),
        needs_more_context=needs_more_context,
    )
