import pytest

from src.ai.evaluation import evaluate_summary
from src.ai.schemas import parse_copilot_response
from src.bq1.contracts import BQ1MetadataResult
from src.context.builder import build_case_context
from src.context.projection import build_llm_input
from tests.unit.context_fixtures import (
    make_context,
    make_handoff,
    make_raw_edges,
)

# The summary-v1 answer Nova 2 Lite produced for this fixture case.
V1_ANSWER = (
    "The alert is transaction_id txn_synthetic_alert from source_account_key "
    "SYNTHETIC_ACCOUNT_A to target_account_key SYNTHETIC_ACCOUNT_B for USD "
    "100.25 via WIRE on 2022-09-03T12:00:00Z. BQ1 metadata is available and "
    "shows a risk_rank of 12. The queried window of 48 hours contains 2 "
    "upstream and 3 downstream connected accounts. The first candidate by "
    "candidate_rank is account_key SYNTHETIC_UPSTREAM_1 with ranking inputs of "
    "closest_gap_minutes 180 and supporting_transaction_count 3. There is a "
    "repeated relationship between SYNTHETIC_ACCOUNT_A and SYNTHETIC_UPSTREAM_1."
)
GOOD_ANSWER = (
    "Alert txn_synthetic_alert is a WIRE payment of USD 100.25 from "
    "SYNTHETIC_ACCOUNT_A to SYNTHETIC_ACCOUNT_B on 2022-09-03 at 12:00 UTC. "
    "BQ1 ranks it 12 overall. The 48-hour window contains 2 upstream and 3 "
    "downstream connected accounts. SYNTHETIC_UPSTREAM_1 is first in review "
    "order, with 3 supporting transfers and a closest transfer 180 minutes from "
    "the alert. It appears on both sides: it paid the sender and received money "
    "from the receiver. SYNTHETIC_DOWNSTREAM_1 also has repeated transfers, "
    "with 2 supporting transfers."
)


def response(answer):
    return parse_copilot_response(
        {
            "answer": answer,
            "evidence": [],
            "limitations": [],
            "needs_more_context": False,
        }
    )


@pytest.fixture
def llm_input():
    return build_llm_input(make_context())


def test_complete_plain_summary_passes_every_check(llm_input):
    quality = evaluate_summary(response(GOOD_ANSWER), llm_input)

    assert quality.passed, quality.failed_checks
    assert quality.sentence_count == 6


def test_v1_summary_misses_patterns_and_uses_field_names(llm_input):
    quality = evaluate_summary(response(V1_ANSWER), llm_input)

    assert quality.failed_checks == [
        "mentions_repeated_candidates",
        "no_field_names",
    ]
    assert quality.checks["mentions_two_way_candidates"] is True


def test_missing_bq1_must_be_stated_as_unavailable():
    context = build_case_context(
        handoff=make_handoff(),
        raw_edges=make_raw_edges(),
        bq1_result=BQ1MetadataResult.unavailable("Not loaded."),
        queried_window_hours=48,
    )
    llm_input = build_llm_input(context)
    silent = GOOD_ANSWER.replace("BQ1 ranks it 12 overall. ", "")
    stated = silent.replace(
        "The 48-hour", "BQ1 metadata is not available. The 48-hour"
    )

    assert evaluate_summary(response(silent), llm_input).checks["covers_bq1"] is False
    assert evaluate_summary(response(stated), llm_input).checks["covers_bq1"] is True


def test_case_without_neighbours_skips_candidate_checks():
    context = build_case_context(
        handoff=make_handoff(),
        raw_edges=make_raw_edges().iloc[:1],
        bq1_result=BQ1MetadataResult.unavailable("Not loaded."),
        queried_window_hours=48,
    )
    answer = (
        "Alert txn_synthetic_alert is a WIRE payment of USD 100.25. BQ1 "
        "metadata is not available. The window has 0 upstream and 0 "
        "downstream connected accounts."
    )

    in_words = answer.replace(
        "0 upstream and 0 downstream", "no upstream or downstream"
    )
    llm_input = build_llm_input(context)

    quality = evaluate_summary(response(answer), llm_input)

    assert quality.passed, quality.failed_checks
    assert quality.checks["mentions_first_candidate"] is None
    assert evaluate_summary(response(in_words), llm_input).passed


def test_no_wording_does_not_cover_a_nonzero_count(llm_input):
    answer = GOOD_ANSWER.replace("2 upstream and 3", "no upstream and 3")

    quality = evaluate_summary(response(answer), llm_input)

    assert quality.failed_checks == ["covers_case_size"]


def test_numbers_written_as_words_fail_the_digits_check(llm_input):
    answer = GOOD_ANSWER.replace("2 upstream and 3", "two upstream and three")

    quality = evaluate_summary(response(answer), llm_input)

    assert quality.failed_checks == ["covers_case_size", "numbers_as_digits"]


def test_guilt_language_is_flagged(llm_input):
    answer = GOOD_ANSWER + " SYNTHETIC_UPSTREAM_1 is laundering money."

    quality = evaluate_summary(response(answer), llm_input)

    assert quality.failed_checks == ["no_guilt_language"]
