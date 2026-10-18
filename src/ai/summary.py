"""Grounded case summary: prompt, call, parse, verify, retry once."""

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from src.ai.grounding import (
    GroundingIssue,
    build_grounding_index,
    check_response,
)
from src.ai.llm_client import (
    LLMUsage,
    StructuredCompletion,
    StructuredLLMClient,
    total_usage,
)
from src.ai.prompts import (
    ANSWER_TOOL,
    DEFAULT_SUMMARY_PROMPT,
    SummaryPrompt,
    build_retry_message,
    build_user_message,
)
from src.ai.schemas import (
    CopilotResponse,
    CopilotResponseSchemaError,
    parse_copilot_response,
)
from src.context.validation import serialize_case_context


@dataclass(frozen=True)
class SummaryAttempt:
    completion: StructuredCompletion
    issues: tuple[GroundingIssue, ...]


@dataclass(frozen=True)
class CaseSummaryResult:
    """Outcome of a summary request.

    status is "grounded" when an answer passed every check, and "rejected"
    when every attempt failed; a rejected result carries no answer to show.
    """

    status: Literal["grounded", "rejected"]
    response: CopilotResponse | None
    attempts: tuple[SummaryAttempt, ...]
    model_id: str
    prompt_version: str
    context_sha256: str

    @property
    def usage(self) -> LLMUsage:
        return total_usage([attempt.completion for attempt in self.attempts])


def _evaluate(
    completion: StructuredCompletion,
    index,
) -> tuple[CopilotResponse | None, tuple[GroundingIssue, ...]]:
    if completion.payload is None:
        return None, (
            GroundingIssue(
                "no_structured_output",
                "The model did not call submit_answer.",
            ),
        )
    try:
        response = parse_copilot_response(completion.payload)
    except CopilotResponseSchemaError as error:
        return None, (GroundingIssue("schema_error", str(error)),)
    return response, check_response(response, index).issues


def generate_case_summary(
    llm_input: dict[str, Any],
    client: StructuredLLMClient,
    *,
    prompt: SummaryPrompt = DEFAULT_SUMMARY_PROMPT,
    max_attempts: int = 2,
    max_tokens: int = 800,
    temperature: float = 0.0,
) -> CaseSummaryResult:
    """Summarise a bounded CaseContext, returning only a verified answer.

    Provider failures raise LLMClientError, and invalid contexts raise
    CaseContextError, so callers can show them distinctly.
    """

    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be an integer >= 1.")

    index = build_grounding_index(llm_input)
    context_json = serialize_case_context(llm_input)
    user_message = build_user_message(
        context_json=context_json,
        index=index,
        task=prompt.task,
    )

    attempts: list[SummaryAttempt] = []
    message = user_message
    for _ in range(max_attempts):
        completion = client.complete_structured(
            system=prompt.system,
            user_message=message,
            tool=ANSWER_TOOL,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        response, issues = _evaluate(completion, index)
        attempts.append(SummaryAttempt(completion=completion, issues=issues))
        if response is not None and not issues:
            break
        message = build_retry_message(
            user_message=user_message,
            previous_payload=completion.payload,
            problems=issues,
        )

    grounded = not attempts[-1].issues
    return CaseSummaryResult(
        status="grounded" if grounded else "rejected",
        response=response if grounded else None,
        attempts=tuple(attempts),
        model_id=client.model_id,
        prompt_version=prompt.version,
        context_sha256=hashlib.sha256(context_json.encode("utf-8")).hexdigest(),
    )
