"""Deterministic connected-account facts shown beside the AI summary.

The summary names only the first candidate; the pattern sentence and review
table cover every candidate in the bounded CaseContext, so per-account facts
never depend on model wording.
"""

from collections.abc import Mapping, Sequence
from typing import Any

PATTERN_LIST_LIMIT = 5

_SIDE_LABELS = {
    "upstream": "Upstream (paid the sender)",
    "downstream": "Downstream (received from the receiver)",
    "both": "Both sides",
}


def build_review_table(llm_input: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return one display row per candidate, in the supplied review order."""

    rows = []
    for candidate in llm_input["bq2"]["candidates"]:
        rows.append(
            {
                "review_order": candidate["candidate_rank"],
                "account_key": candidate["account_key"],
                "side": _SIDE_LABELS[candidate["relationship_direction"]],
                "supporting_transfers": candidate["supporting_transaction_count"],
                "upstream_transfers": candidate[
                    "upstream_supporting_transaction_count"
                ],
                "downstream_transfers": candidate[
                    "downstream_supporting_transaction_count"
                ],
                "repeated_transfers": candidate["repeated_transfer_indicator"],
                "closest_gap_minutes": candidate["closest_gap_minutes"],
                "payment_formats": ", ".join(candidate["payment_formats"]),
                "amounts_by_currency": ", ".join(
                    f"{currency} {amount}"
                    for currency, amount in candidate[
                        "amount_totals_by_currency"
                    ].items()
                ),
                "details_complete": candidate["transaction_details_complete"],
            }
        )
    return rows


def _transfer_count(candidate: Mapping[str, Any]) -> str:
    count = candidate["supporting_transaction_count"]
    return f"{count} supporting transfer{'' if count == 1 else 's'}"


def _pattern_sentence(
    label: str,
    candidates: Sequence[Mapping[str, Any]],
    none_text: str,
) -> str:
    if not candidates:
        return none_text
    listed = [
        f"{candidate['account_key']} ({_transfer_count(candidate)})"
        for candidate in candidates[:PATTERN_LIST_LIMIT]
    ]
    remaining = len(candidates) - PATTERN_LIST_LIMIT
    if remaining > 0:
        listed.append(f"and {remaining} more in the review table")
    return f"{label}: {', '.join(listed)}."


def build_pattern_summary(llm_input: Mapping[str, Any]) -> str | None:
    """Describe both-sided and repeated-transfer candidates from the data.

    Returns None when the case has no connected accounts at all.
    """

    candidates = llm_input["bq2"]["candidates"]
    omitted = llm_input["evidence_truncation"]["omitted_candidate_count"]
    if not candidates and not omitted:
        return None

    sentences = [
        _pattern_sentence(
            "Accounts on both sides (paid the sender and received money "
            "from the receiver)",
            [
                candidate
                for candidate in candidates
                if candidate["relationship_direction"] == "both"
            ],
            "No listed connected account appears on both sides.",
        ),
        _pattern_sentence(
            "Accounts with repeated transfers",
            [
                candidate
                for candidate in candidates
                if candidate["repeated_transfer_indicator"]
            ],
            "No listed connected account made repeated transfers.",
        ),
    ]
    if omitted:
        sentences.append(
            f"{omitted} more connected account{'' if omitted == 1 else 's'} "
            "could not be included in this view, so these lists may be "
            "incomplete."
        )
    return " ".join(sentences)
