"""Deterministic quality checks for grounded case summaries.

Grounding (src.ai.grounding) decides whether an answer may be shown. These
checks measure what grounding cannot: whether a summary covers the facts an
analyst needs, and whether it follows the wording rules.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from src.ai.grounding import numbers_in_text
from src.ai.schemas import CopilotResponse

_SNAKE_CASE_TOKEN = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_GUILT_LANGUAGE = re.compile(
    r"\b(guilty|criminal|laundered|is laundering|are laundering|"
    r"confirmed suspicious|definitely suspicious)\b",
    re.IGNORECASE,
)
_UNAVAILABLE_WORDING = re.compile(r"\b(not available|unavailable)\b", re.IGNORECASE)
# "one" is left out because it is also a pronoun ("the first one").
_NUMBER_WORDS = re.compile(
    r"\b(zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


@dataclass(frozen=True)
class SummaryQuality:
    """Each check is True or False, or None when it does not apply."""

    checks: Mapping[str, bool | None]
    sentence_count: int

    @property
    def passed(self) -> bool:
        return all(result is not False for result in self.checks.values())

    @property
    def failed_checks(self) -> list[str]:
        return [name for name, result in self.checks.items() if result is False]


def _collect_keys(value: Any, keys: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            keys.add(key)
            _collect_keys(item, keys)
    elif isinstance(value, list):
        for item in value:
            _collect_keys(item, keys)


def _mentions_all(answer: str, account_keys: list[str]) -> bool | None:
    if not account_keys:
        return None
    return all(account_key in answer for account_key in account_keys)


def _states_count(answer: str, count: int, side: str) -> bool:
    if re.search(rf"\b{count}\s+{side}\b", answer):
        return True
    # "no upstream or downstream connected accounts" states a zero count.
    return count == 0 and bool(
        re.search(rf"\bno\b[\w\s,]*\b{side}\b", answer, re.IGNORECASE)
    )


def evaluate_summary(
    response: CopilotResponse,
    llm_input: Mapping[str, Any],
) -> SummaryQuality:
    """Score a grounded summary against the facts in its bounded CaseContext."""

    answer = response.answer
    numbers = numbers_in_text(answer)
    statistics = llm_input["bq2"]["case_statistics"]
    candidates = llm_input["bq2"]["candidates"]
    bq1 = llm_input["bq1"]

    if bq1["status"] == "available":
        rank = Decimal(bq1["metadata"]["risk_rank"])
        covers_bq1 = rank in numbers
    else:
        covers_bq1 = bool(_UNAVAILABLE_WORDING.search(answer))

    context_keys: set[str] = set()
    _collect_keys(llm_input, context_keys)
    field_names = {
        token
        for token in _SNAKE_CASE_TOKEN.findall(answer)
        if token in context_keys
    }

    checks = {
        "mentions_alert_id": llm_input["alert"]["transaction_id"] in answer,
        "covers_bq1": covers_bq1,
        "covers_case_size": all(
            _states_count(answer, statistics[f"{side}_connected_account_count"], side)
            for side in ("upstream", "downstream")
        ),
        "mentions_first_candidate": _mentions_all(
            answer,
            [candidate["account_key"] for candidate in candidates[:1]],
        ),
        "mentions_two_way_candidates": _mentions_all(
            answer,
            [
                candidate["account_key"]
                for candidate in candidates
                if candidate["relationship_direction"] == "both"
            ],
        ),
        "mentions_repeated_candidates": _mentions_all(
            answer,
            [
                candidate["account_key"]
                for candidate in candidates
                if candidate["repeated_transfer_indicator"]
            ],
        ),
        "no_field_names": not field_names,
        "numbers_as_digits": not _NUMBER_WORDS.search(answer),
        "no_guilt_language": not _GUILT_LANGUAGE.search(answer),
    }
    return SummaryQuality(
        checks=checks,
        sentence_count=len(_SENTENCE_END.findall(answer)),
    )
