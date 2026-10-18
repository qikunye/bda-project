"""Grounded case summary: prompt, call, parse, verify, retry once."""

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from src.ai.evaluation import evaluate_summary
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
from src.ai.review import build_pattern_summary
from src.ai.schemas import (
    CopilotResponse,
    CopilotResponseSchemaError,
    parse_copilot_response,
)
from src.context.validation import serialize_case_context

# Wording problems that are worth one retry but never block a grounded answer.
_STYLE_MESSAGES = {
    "no_field_names": (
        "Answer uses JSON field names; write plain words such as "
        '"the sender" or "review order" instead.'
    ),
    "numbers_as_digits": (
        "Answer writes numbers as words; write every number as digits "
        "(3, not three)."
    ),
}


_PATTERN_CLAIM = re.compile(
    r"\b(both sides|repeated|repeats?|no other (?:candidate|account)s?|"
    r"only (?:candidate|account))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SummaryAttempt:
    completion: StructuredCompletion
    response: CopilotResponse | None
    issues: tuple[GroundingIssue, ...]
    style_issues: tuple[GroundingIssue, ...] = ()

    @property
    def grounded(self) -> bool:
        return self.response is not None and not self.issues


@dataclass(frozen=True)
class CaseSummaryResult:
    """Outcome of a summary request.

    status is "grounded" when an answer passed every grounding check, and
    "rejected" when every attempt failed; a rejected result carries no answer
    to show. style_issues lists wording problems left in the shown answer.
    pattern_summary is computed from the data, not by the model, and is
    present whatever the status (None only when there are no connected
    accounts).
    """

    status: Literal["grounded", "rejected"]
    response: CopilotResponse | None
    style_issues: tuple[GroundingIssue, ...]
    pattern_summary: str | None
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


def _style_issues(
    response: CopilotResponse,
    llm_input: dict[str, Any],
) -> tuple[GroundingIssue, ...]:
    checks = evaluate_summary(response, llm_input).checks
    return tuple(
        GroundingIssue(code, message)
        for code, message in _STYLE_MESSAGES.items()
        if checks[code] is False
    )


def _scope_issues(
    response: CopilotResponse,
    llm_input: dict[str, Any],
    prompt: SummaryPrompt,
) -> tuple[GroundingIssue, ...]:
    """Block answers that describe what the application computes itself."""

    issues = []
    if prompt.max_named_candidates is not None:
        candidates = [
            candidate["account_key"] for candidate in llm_input["bq2"]["candidates"]
        ]
        allowed = set(candidates[: prompt.max_named_candidates])
        for account_key in candidates:
            if account_key not in allowed and account_key in response.answer:
                issues.append(
                    GroundingIssue(
                        "out_of_scope_account",
                        f"Answer names connected account {account_key!r}; "
                        "name only the first candidate in review order.",
                    )
                )
    if not prompt.allows_pattern_claims:
        match = _PATTERN_CLAIM.search(response.answer)
        if match:
            issues.append(
                GroundingIssue(
                    "out_of_scope_pattern_claim",
                    f"Answer describes account patterns ({match.group()!r}); "
                    "leave them out, the application adds them from the data.",
                )
            )
    return tuple(issues)


def _answer_to_show(attempts: list[SummaryAttempt]) -> SummaryAttempt | None:
    grounded = [attempt for attempt in attempts if attempt.grounded]
    clean = [attempt for attempt in grounded if not attempt.style_issues]
    if clean:
        return clean[0]
    return grounded[-1] if grounded else None


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
    if prompt.instruction_numbers:
        index = replace(index, numbers=index.numbers | prompt.instruction_numbers)
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
        if response is not None:
            issues = (*issues, *_scope_issues(response, llm_input, prompt))
        style_issues = (
            _style_issues(response, llm_input)
            if response is not None and not issues
            else ()
        )
        attempts.append(
            SummaryAttempt(
                completion=completion,
                response=response,
                issues=issues,
                style_issues=style_issues,
            )
        )
        if response is not None and not issues and not style_issues:
            break
        message = build_retry_message(
            user_message=user_message,
            previous_payload=completion.payload,
            problems=[*issues, *style_issues],
        )

    shown = _answer_to_show(attempts)
    return CaseSummaryResult(
        status="grounded" if shown is not None else "rejected",
        response=None if shown is None else shown.response,
        style_issues=() if shown is None else shown.style_issues,
        pattern_summary=build_pattern_summary(llm_input),
        attempts=tuple(attempts),
        model_id=client.model_id,
        prompt_version=prompt.version,
        context_sha256=hashlib.sha256(context_json.encode("utf-8")).hexdigest(),
    )
