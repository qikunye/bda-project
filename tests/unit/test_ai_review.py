from src.ai.review import build_pattern_summary, build_review_table
from src.context.projection import build_llm_input
from tests.unit.context_fixtures import make_context


def test_review_table_lists_every_candidate_in_review_order():
    llm_input = build_llm_input(make_context())

    table = build_review_table(llm_input)

    assert [row["account_key"] for row in table] == [
        candidate["account_key"] for candidate in llm_input["bq2"]["candidates"]
    ]
    assert [row["review_order"] for row in table] == list(
        range(1, len(table) + 1)
    )


def test_review_table_carries_each_accounts_own_facts():
    first = build_review_table(build_llm_input(make_context()))[0]

    assert first == {
        "review_order": 1,
        "account_key": "SYNTHETIC_UPSTREAM_1",
        "side": "Both sides",
        "supporting_transfers": 3,
        "upstream_transfers": 2,
        "downstream_transfers": 1,
        "repeated_transfers": True,
        "closest_gap_minutes": 180,
        "payment_formats": "ACH, CARD, WIRE",
        "amounts_by_currency": "SGD 60.00, USD 55.75",
        "details_complete": True,
    }


def test_truncated_projection_marks_incomplete_details():
    llm_input = build_llm_input(make_context(), max_candidates=4, max_transactions=3)

    table = build_review_table(llm_input)

    assert len(table) == 4
    assert any(row["details_complete"] is False for row in table)


def test_pattern_summary_lists_both_sided_and_repeated_accounts():
    summary = build_pattern_summary(build_llm_input(make_context()))

    assert summary == (
        "Accounts on both sides (paid the sender and received money from the "
        "receiver): SYNTHETIC_UPSTREAM_1 (3 supporting transfers). "
        "Accounts with repeated transfers: SYNTHETIC_UPSTREAM_1 "
        "(3 supporting transfers), SYNTHETIC_DOWNSTREAM_1 "
        "(2 supporting transfers)."
    )


def test_pattern_summary_states_none_and_discloses_omitted_accounts():
    llm_input = build_llm_input(make_context(), max_candidates=1, max_transactions=100)
    llm_input["bq2"]["candidates"][0]["relationship_direction"] = "upstream"
    llm_input["bq2"]["candidates"][0]["repeated_transfer_indicator"] = False

    summary = build_pattern_summary(llm_input)

    assert summary == (
        "No listed connected account appears on both sides. "
        "No listed connected account made repeated transfers. "
        "3 more connected accounts could not be included in this view, so "
        "these lists may be incomplete."
    )


def test_long_pattern_lists_point_to_the_review_table():
    llm_input = build_llm_input(make_context())
    template = llm_input["bq2"]["candidates"][1]
    llm_input["bq2"]["candidates"] = [
        {**template, "account_key": f"SYNTHETIC_EXTRA_{number}"}
        for number in range(7)
    ]

    summary = build_pattern_summary(llm_input)

    assert summary.endswith(
        "SYNTHETIC_EXTRA_4 (2 supporting transfers), and 2 more in the "
        "review table."
    )


def test_case_without_connected_accounts_has_no_pattern_summary():
    llm_input = build_llm_input(make_context(), max_candidates=0)
    llm_input["evidence_truncation"]["omitted_candidate_count"] = 0
    llm_input["bq2"]["candidates"] = []

    assert build_pattern_summary(llm_input) is None
