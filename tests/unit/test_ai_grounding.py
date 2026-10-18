from copy import deepcopy

import pytest

from src.ai.grounding import build_grounding_index, check_response
from src.ai.schemas import parse_copilot_response
from src.context.projection import build_llm_input
from src.context.validation import CaseContextIntegrityError
from tests.unit.context_fixtures import make_context

ALERT_ID = "txn_synthetic_alert"
TOP_CANDIDATE = "SYNTHETIC_UPSTREAM_1"
REQUIRED_LIMITATIONS = [
    {"code": "missing_amounts", "detail": "One supporting amount is missing."},
    {"code": "invalid_amounts", "detail": "One supporting amount is invalid."},
]


@pytest.fixture
def llm_input():
    return build_llm_input(make_context(), max_candidates=20, max_transactions=100)


@pytest.fixture
def index(llm_input):
    return build_grounding_index(llm_input)


def make_response(**overrides):
    payload = {
        "answer": (
            f"Alert {ALERT_ID} was a WIRE payment of USD 100.25 on "
            "2022-09-03 at 12:00 UTC, ranked 12 by BQ1. "
            f"{TOP_CANDIDATE} is the first connected account to review, "
            "with 3 supporting transactions and a closest gap of 180 minutes."
        ),
        "evidence": [
            {"type": "transaction", "id": ALERT_ID},
            {"type": "account", "id": TOP_CANDIDATE},
        ],
        "limitations": deepcopy(REQUIRED_LIMITATIONS),
        "needs_more_context": False,
    }
    payload.update(overrides)
    return parse_copilot_response(payload)


def issue_codes(report):
    return [issue.code for issue in report.issues]


def test_grounded_answer_passes(index):
    report = check_response(make_response(), index)

    assert report.ok, report.issues


def test_index_requires_bounded_llm_input():
    with pytest.raises(CaseContextIntegrityError):
        build_grounding_index(make_context())


def test_cited_id_absent_from_case_is_rejected(index):
    response = make_response(
        evidence=[
            {"type": "transaction", "id": ALERT_ID},
            {"type": "account", "id": TOP_CANDIDATE},
            {"type": "transaction", "id": "txn_invented"},
        ]
    )

    assert issue_codes(check_response(response, index)) == [
        "unknown_evidence_id"
    ]


def test_cited_id_under_wrong_type_is_rejected(index):
    response = make_response(
        evidence=[
            {"type": "account", "id": ALERT_ID},
            {"type": "account", "id": TOP_CANDIDATE},
        ]
    )

    assert "unknown_evidence_id" in issue_codes(check_response(response, index))


def test_id_mentioned_in_text_but_absent_from_case_is_rejected(index):
    response = make_response(
        answer=f"Alert {ALERT_ID} was also linked to SYNTHETIC_ACCOUNT_Z.",
        evidence=[{"type": "transaction", "id": ALERT_ID}],
    )

    assert issue_codes(check_response(response, index)) == [
        "unknown_mentioned_id"
    ]


def test_id_mentioned_in_text_must_be_cited(index):
    response = make_response(
        evidence=[{"type": "transaction", "id": ALERT_ID}],
    )

    assert issue_codes(check_response(response, index)) == [
        "uncited_mentioned_id"
    ]


def test_invented_number_is_rejected(index):
    response = make_response(
        answer=f"Alert {ALERT_ID} was a WIRE payment of USD 999.99.",
        evidence=[{"type": "transaction", "id": ALERT_ID}],
    )

    report = check_response(response, index)

    assert issue_codes(report) == ["unsupported_number"]
    assert "999.99" in report.issues[0].message


def test_cross_currency_sum_is_rejected(index, llm_input):
    totals = llm_input["bq2"]["case_statistics"]["amount_totals_by_currency"]
    assert (totals["USD"], totals["SGD"]) == ("156.00", "60.00")
    response = make_response(
        answer=f"Alert {ALERT_ID}'s case moved 216.00 in total across USD and SGD.",
        evidence=[{"type": "transaction", "id": ALERT_ID}],
    )

    assert issue_codes(check_response(response, index)) == [
        "unsupported_number"
    ]


def test_score_restated_as_percentage_is_rejected(index):
    response = make_response(
        answer=f"Alert {ALERT_ID} has an 84% chance of being laundering.",
        evidence=[{"type": "transaction", "id": ALERT_ID}],
    )

    assert issue_codes(check_response(response, index)) == [
        "unsupported_number"
    ]


@pytest.mark.parametrize(
    ("words", "accepted"),
    [
        ("two upstream and three downstream", True),
        ("seventeen upstream accounts", False),
        ("Forty-Eight hours", True),
        ("twenty-five accounts", False),
    ],
)
def test_numbers_written_as_words_are_checked(index, words, accepted):
    response = make_response(
        answer=f"Alert {ALERT_ID} has {words}.",
        evidence=[{"type": "transaction", "id": ALERT_ID}],
    )

    assert check_response(response, index).ok is accepted


def test_formatted_numbers_match_case_values(index):
    response = make_response(
        answer=f"Alert {ALERT_ID} paid USD 100.250, and the case spans 1,440 minutes.",
        evidence=[{"type": "transaction", "id": ALERT_ID}],
    )

    assert check_response(response, index).ok


def test_digits_inside_identifiers_are_not_treated_as_numbers(index):
    relationship_id = next(iter(index.ids_by_type["relationship"]))
    response = make_response(
        answer=f"Relationship {relationship_id} includes {ALERT_ID}.",
        evidence=[
            {"type": "relationship", "id": relationship_id},
            {"type": "transaction", "id": ALERT_ID},
        ],
    )

    assert check_response(response, index).ok


def test_limitation_details_are_checked_like_the_answer(index):
    response = make_response(
        limitations=[
            REQUIRED_LIMITATIONS[0],
            {"code": "invalid_amounts", "detail": "There are 17 invalid amounts."},
        ]
    )

    assert issue_codes(check_response(response, index)) == [
        "unsupported_number"
    ]


def test_answer_without_evidence_must_request_more_context(index):
    unanswerable = "The current case context does not cover activity last month."

    rejected = make_response(answer=unanswerable, evidence=[])
    accepted = make_response(
        answer=unanswerable,
        evidence=[],
        needs_more_context=True,
    )

    assert issue_codes(check_response(rejected, index)) == ["missing_evidence"]
    assert check_response(accepted, index).ok


def test_missing_required_disclosure_is_rejected(index):
    response = make_response(limitations=REQUIRED_LIMITATIONS[:1])

    report = check_response(response, index)

    assert issue_codes(report) == ["missing_disclosure"]
    assert "invalid_amounts" in report.issues[0].message


def test_truncated_case_requires_truncation_disclosure():
    truncated = build_llm_input(
        make_context(),
        max_candidates=4,
        max_transactions=3,
    )
    index = build_grounding_index(truncated)

    assert issue_codes(check_response(make_response(), index)) == [
        "missing_disclosure"
    ]


def test_disclosure_that_does_not_hold_is_rejected(index):
    response = make_response(
        limitations=[
            *REQUIRED_LIMITATIONS,
            {"code": "evidence_truncated", "detail": "Some evidence was cut."},
        ]
    )

    assert issue_codes(check_response(response, index)) == ["false_disclosure"]


def test_optional_true_disclosures_and_other_are_accepted(index):
    response = make_response(
        limitations=[
            *REQUIRED_LIMITATIONS,
            {
                "code": "model_explanations_unavailable",
                "detail": "BQ1 reasons are not available.",
            },
            {"code": "wider_window_not_queried", "detail": "72h not queried."},
            {"code": "other", "detail": "Synthetic dataset."},
        ]
    )

    assert check_response(response, index).ok


def test_id_only_transaction_reference_is_valid_evidence():
    truncated = build_llm_input(
        make_context(),
        max_candidates=4,
        max_transactions=3,
    )
    embedded = {
        transaction["transaction_id"]
        for transaction in truncated["bq2"]["transactions"]
    }
    id_only = next(
        transaction_id
        for candidate in truncated["bq2"]["candidates"]
        for transaction_id in candidate["id_only_transaction_ids"]
    )
    assert id_only not in embedded
    response = make_response(
        answer=f"Transaction {id_only} is referenced but its details were omitted.",
        evidence=[{"type": "transaction", "id": id_only}],
        limitations=[
            *REQUIRED_LIMITATIONS,
            {"code": "evidence_truncated", "detail": "Details were omitted."},
        ],
    )

    assert check_response(response, build_grounding_index(truncated)).ok


DOWNSTREAM_1 = "SYNTHETIC_DOWNSTREAM_1"


def pairing_response(sentence):
    return make_response(
        answer=f"Alert {ALERT_ID} has connected accounts. {sentence}",
        evidence=[
            {"type": "transaction", "id": ALERT_ID},
            {"type": "account", "id": TOP_CANDIDATE},
            {"type": "account", "id": DOWNSTREAM_1},
            {"type": "account", "id": "SYNTHETIC_ACCOUNT_A"},
        ],
    )


@pytest.mark.parametrize(
    "sentence",
    [
        f"{TOP_CANDIDATE} made 3 supporting transfers.",
        f"{TOP_CANDIDATE} has 3 supporting transfers and {DOWNSTREAM_1} has 2.",
        f"{TOP_CANDIDATE} made 3 transfers, and {DOWNSTREAM_1} made 2 transfers.",
        # A side-specific count (2 upstream transfers) is that account's count too.
        f"{TOP_CANDIDATE} made 2 transfers into the sender.",
        # Counts after a non-candidate account are not attributed.
        "SYNTHETIC_ACCOUNT_A received 4 transfers.",
        # Counts in a sentence without an account are not attributed.
        "There were 8 transactions in the case.",
    ],
)
def test_correctly_paired_or_unattributable_counts_pass(index, sentence):
    assert check_response(pairing_response(sentence), index).ok


@pytest.mark.parametrize(
    "sentence",
    [
        f"{DOWNSTREAM_1} made 3 supporting transfers.",
        f"{TOP_CANDIDATE} has 2 supporting transfers and {DOWNSTREAM_1} made 3 transfers.",
        f"{DOWNSTREAM_1} made three supporting transfers.",
    ],
)
def test_count_attached_to_the_wrong_account_is_rejected(index, sentence):
    report = check_response(pairing_response(sentence), index)

    assert issue_codes(report) == ["misattributed_count"]
    assert DOWNSTREAM_1 in report.issues[0].message


def test_bare_number_list_is_rejected(index):
    # The pattern Nova produced on a real 7-account case.
    sentence = "The downstream candidates made 6, 4, 2, 2, and 1 transfers."

    report = check_response(
        pairing_response(sentence.replace("6, 4, 2, 2, and 1", "3, 2, 2, and 1")),
        index,
    )

    assert issue_codes(report) == ["unattributed_number_list"]


def test_dates_and_amounts_are_not_number_lists(index):
    sentence = "The alert was on September 3, 2022 for USD 1,440."

    assert check_response(pairing_response(sentence), index).ok
