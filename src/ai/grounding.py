"""Deterministic grounding checks for Copilot answers against a bounded CaseContext.

The checks are plain Python over an index built once per case; they make no
model calls. They verify that cited and mentioned identifiers exist in the
case, that every number in the answer text appears somewhere in the case, and
that disclosure codes match the case's data-quality conditions.

Known limit: the number check is set membership. It rejects invented numbers
(including cross-currency sums), but not a real number attached to the wrong
fact. The one attribution it does check is a transfer count stated after a
connected account in the same sentence, which must be that account's count;
lists of three or more bare numbers are rejected because they cannot be
attributed. Wording rules such as "no guilt claims" are enforced by the prompt
and measured by evaluation, not here.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from src.ai.schemas import CopilotResponse
from src.context.validation import validate_llm_input

# Identifier shapes that may appear in answer text. Real account keys look
# like "120075|81294D7A0"; synthetic fixtures use "SYNTHETIC_..." keys.
_ID_MENTION_PATTERNS = (
    ("transaction", re.compile(r"\btxn_[A-Za-z0-9_]+")),
    ("relationship", re.compile(r"\brel_[0-9a-f]+")),
    ("account", re.compile(r"\b\d+\|[0-9A-Za-z]+")),
    ("account", re.compile(r"\bSYNTHETIC_[A-Z0-9_]+")),
)
_NUMBER_PATTERN = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")

# Numbers written as words must not bypass the number check.
_UNIT_WORDS = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
]
_TENS_WORDS = [
    "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
]
_NUMBER_WORD_PATTERN = re.compile(
    rf"\b(?:({'|'.join(_TENS_WORDS)})(?:-({'|'.join(_UNIT_WORDS[1:10])}))?"
    rf"|({'|'.join(_UNIT_WORDS)}))\b",
    re.IGNORECASE,
)

_ACCOUNT_MENTION_PATTERN = re.compile(
    "|".join(
        pattern.pattern
        for evidence_type, pattern in _ID_MENTION_PATTERNS
        if evidence_type == "account"
    )
)
_NUMBER_WORDS_ALTERNATION = (
    rf"(?:{'|'.join(_TENS_WORDS)})(?:-(?:{'|'.join(_UNIT_WORDS[1:10])}))?"
    rf"|{'|'.join(_UNIT_WORDS)}"
)
_TRANSFER_COUNT_PATTERN = re.compile(
    rf"\b(\d[\d,]*|{_NUMBER_WORDS_ALTERNATION})\s+"
    r"(?:supporting\s+)?(?:transfers?|transactions?)\b",
    re.IGNORECASE,
)
_NUMBER_LIST_PATTERN = re.compile(r"\b\d+(?:,\s+(?:and\s+)?\d+){2,}\b")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class GroundingIssue:
    code: str
    message: str


@dataclass(frozen=True)
class GroundingReport:
    issues: tuple[GroundingIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class GroundingIndex:
    """Lookup sets for one bounded CaseContext."""

    ids_by_type: Mapping[str, frozenset[str]]
    numbers: frozenset[Decimal]
    true_disclosures: frozenset[str]
    required_disclosures: frozenset[str]
    candidate_transfer_counts: Mapping[str, frozenset[Decimal]]


def _normalize_number(token: str) -> Decimal | None:
    try:
        return Decimal(token.rstrip(",").replace(",", "")).normalize()
    except InvalidOperation:
        return None


def numbers_in_text(text: str, *, include_words: bool = False) -> set[Decimal]:
    numbers = set()
    for token in _NUMBER_PATTERN.findall(text):
        number = _normalize_number(token)
        if number is not None:
            numbers.add(number)
    if include_words:
        for tens, unit, small in _NUMBER_WORD_PATTERN.findall(text):
            if small:
                value = _UNIT_WORDS.index(small.lower())
            else:
                value = 10 * (_TENS_WORDS.index(tens.lower()) + 2)
                value += _UNIT_WORDS.index(unit.lower()) if unit else 0
            numbers.add(Decimal(value).normalize())
    return numbers


def _collect_numbers(value: Any, numbers: set[Decimal]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        numbers.add(Decimal(str(value)).normalize())
    elif isinstance(value, str):
        numbers.update(numbers_in_text(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            _collect_numbers(item, numbers)
    elif isinstance(value, list):
        for item in value:
            _collect_numbers(item, numbers)


def _account_keys(context: Mapping[str, Any]) -> set[str]:
    bq2 = context["bq2"]
    records = [context["alert"], *bq2["transactions"], *bq2["relationships"]]
    accounts = {
        record[key]
        for record in records
        for key in ("source_account_key", "target_account_key")
    }
    accounts.update(candidate["account_key"] for candidate in bq2["candidates"])
    return accounts


def _transaction_ids(context: Mapping[str, Any]) -> set[str]:
    # ID-only references are valid evidence identifiers even though their
    # details were omitted from the projection.
    bq2 = context["bq2"]
    transaction_ids = {
        transaction["transaction_id"] for transaction in bq2["transactions"]
    }
    for record in [*bq2["relationships"], *bq2["candidates"]]:
        transaction_ids.update(record["transaction_ids"])
    return transaction_ids


def _disclosure_conditions(context: Mapping[str, Any]) -> dict[str, bool]:
    data_quality = context["data_quality"]
    return {
        "evidence_truncated": context["evidence_truncation"]["truncated"],
        "missing_amounts": data_quality["missing_amount_count"] > 0,
        "invalid_amounts": data_quality["invalid_amount_count"] > 0,
        "amounts_without_currency": (
            data_quality["valid_amount_without_currency_count"] > 0
        ),
        "bq1_not_available": context["bq1"]["status"] != "available",
        "model_explanations_unavailable": (
            context["bq1"]["model_explanations"]["status"] != "available"
        ),
        "wider_window_not_queried": bool(
            data_quality["unavailable_window_hours"]
        ),
    }


# Conditions that make the evidence incomplete and must always be disclosed.
_REQUIRED_DISCLOSURES = (
    "evidence_truncated",
    "missing_amounts",
    "invalid_amounts",
    "amounts_without_currency",
    "bq1_not_available",
)


def build_grounding_index(context: dict[str, Any]) -> GroundingIndex:
    """Validate a bounded CaseContext and index it for answer checks."""

    validate_llm_input(context)
    numbers: set[Decimal] = set()
    _collect_numbers(context, numbers)
    conditions = _disclosure_conditions(context)
    true_disclosures = {code for code, holds in conditions.items() if holds}
    return GroundingIndex(
        ids_by_type={
            "transaction": frozenset(_transaction_ids(context)),
            "relationship": frozenset(
                relationship["relationship_id"]
                for relationship in context["bq2"]["relationships"]
            ),
            "account": frozenset(_account_keys(context)),
        },
        numbers=frozenset(numbers),
        true_disclosures=frozenset(true_disclosures),
        required_disclosures=frozenset(
            code for code in _REQUIRED_DISCLOSURES if code in true_disclosures
        ),
        candidate_transfer_counts={
            candidate["account_key"]: frozenset(
                Decimal(candidate[field])
                for field in (
                    "supporting_transaction_count",
                    "upstream_supporting_transaction_count",
                    "downstream_supporting_transaction_count",
                )
            )
            for candidate in context["bq2"]["candidates"]
        },
    )


def _attribution_issues(
    answer: str,
    index: GroundingIndex,
) -> list[GroundingIssue]:
    issues = []
    for match in _NUMBER_LIST_PATTERN.finditer(answer):
        issues.append(
            GroundingIssue(
                "unattributed_number_list",
                f"Answer lists numbers {match.group()!r} without naming the "
                "account each belongs to; name each account next to its count.",
            )
        )
    for sentence in _SENTENCE_BOUNDARY.split(answer):
        accounts = [
            (mention.end(), mention.group())
            for mention in _ACCOUNT_MENTION_PATTERN.finditer(sentence)
        ]
        for match in _TRANSFER_COUNT_PATTERN.finditer(sentence):
            preceding = [
                account for end, account in accounts if end <= match.start()
            ]
            if not preceding or preceding[-1] not in index.candidate_transfer_counts:
                continue
            account = preceding[-1]
            (count,) = numbers_in_text(match.group(1), include_words=True)
            if count not in index.candidate_transfer_counts[account]:
                issues.append(
                    GroundingIssue(
                        "misattributed_count",
                        f"Answer gives {account!r} {match.group()!r}, but "
                        "that account's supporting transfer counts are "
                        f"{sorted(int(value) for value in index.candidate_transfer_counts[account])}.",
                    )
                )
    return issues


def check_response(
    response: CopilotResponse,
    index: GroundingIndex,
) -> GroundingReport:
    """Return every grounding problem in a parsed Copilot response."""

    issues: list[GroundingIssue] = []

    cited = {(reference.type, reference.id) for reference in response.evidence}
    for evidence_type, evidence_id in sorted(cited):
        if evidence_id not in index.ids_by_type[evidence_type]:
            issues.append(
                GroundingIssue(
                    "unknown_evidence_id",
                    f"Cited {evidence_type} {evidence_id!r} is not in the case.",
                )
            )

    # Limitation details are shown to the analyst too, so check them as text.
    text = "\n".join(
        [response.answer, *(limitation.detail for limitation in response.limitations)]
    )
    text_without_ids = text
    for evidence_type, pattern in _ID_MENTION_PATTERNS:
        for mention in sorted(set(pattern.findall(text))):
            if mention not in index.ids_by_type[evidence_type]:
                issues.append(
                    GroundingIssue(
                        "unknown_mentioned_id",
                        f"Answer mentions {evidence_type} {mention!r}, "
                        "which is not in the case.",
                    )
                )
            elif (evidence_type, mention) not in cited:
                issues.append(
                    GroundingIssue(
                        "uncited_mentioned_id",
                        f"Answer mentions {evidence_type} {mention!r} "
                        "without citing it in evidence.",
                    )
                )
        text_without_ids = pattern.sub(" ", text_without_ids)

    stated_numbers = numbers_in_text(text_without_ids, include_words=True)
    for number in sorted(stated_numbers - index.numbers):
        issues.append(
            GroundingIssue(
                "unsupported_number",
                f"Answer states {number:f}, which does not appear in the case.",
            )
        )

    issues.extend(_attribution_issues(response.answer, index))

    if not response.evidence and not response.needs_more_context:
        issues.append(
            GroundingIssue(
                "missing_evidence",
                "Answer cites no evidence and does not ask for more context.",
            )
        )

    claimed = {limitation.code for limitation in response.limitations}
    for code in sorted(index.required_disclosures - claimed):
        issues.append(
            GroundingIssue(
                "missing_disclosure",
                f"Answer must disclose {code!r} for this case.",
            )
        )
    for code in sorted(claimed - index.true_disclosures - {"other"}):
        issues.append(
            GroundingIssue(
                "false_disclosure",
                f"Answer discloses {code!r}, which does not hold for this case.",
            )
        )

    return GroundingReport(issues=tuple(issues))
