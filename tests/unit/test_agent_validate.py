import pytest

from src import config
from src.agent.validate import RunEvidence, validate_decision
from tests.unit.agent_loop_fixtures import (
    alert_result,
    graph_result,
    make_evidence,
    related_result,
    valid_payload,
)


def errors_of(payload, evidence=None):
    result = validate_decision(payload, evidence or make_evidence())
    assert not result.ok
    return " ".join(result.errors)


def test_a_grounded_decision_is_accepted_and_code_sets_the_case_id():
    result = validate_decision(valid_payload(), make_evidence(("txn_a", "txn_b")))
    assert result.ok
    assert result.decision["case_id"] == "case_txn_a"
    assert result.decision["note"] is None


def test_a_single_alert_has_no_case_id():
    payload = valid_payload(verdict="raise")
    result = validate_decision(payload, make_evidence())
    assert result.decision["case_id"] is None


# --- Shape


@pytest.mark.parametrize("payload", [None, [], "raise", 3])
def test_a_decision_must_be_an_object(payload):
    assert "JSON object" in errors_of(payload)


def test_unknown_fields_are_rejected_and_case_id_is_explained():
    message = errors_of(valid_payload(case_id="case_x", reason="because"))
    assert "case_id" in message and "set by code" in message


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"verdict": "arrest"}, "verdict must be one of"),
        ({"verdict": None}, "verdict must be one of"),
        ({"confidence": "certain"}, "confidence must be one of"),
        ({"claims": "FAST_CHAIN"}, "claims must be a list"),
        (
            {"claims": [{"code": "MADE_UP", "evidence_ids": ["txn_low"]}]},
            "Unknown claim code",
        ),
        ({"claims": [{"code": "FAST_CHAIN"}]}, "exactly the fields"),
        (
            {"claims": [{"code": "FAST_CHAIN", "evidence_ids": []}]},
            "at least one evidence ID",
        ),
        (
            {"claims": [{"code": "FAST_CHAIN", "evidence_ids": "txn_low"}]},
            "list of ID strings",
        ),
        ({"note": 5}, "note must be a string"),
    ],
)
def test_malformed_fields_are_rejected(overrides, fragment):
    assert fragment in errors_of(valid_payload(**overrides))


def test_a_claim_code_may_appear_once():
    claims = [
        {"code": "FAST_CHAIN", "evidence_ids": ["txn_low"]},
        {"code": "FAST_CHAIN", "evidence_ids": ["txn_high"]},
    ]
    assert "appears twice" in errors_of(valid_payload(claims=claims))


def test_all_errors_are_reported_together():
    result = validate_decision(
        valid_payload(verdict="arrest", confidence="certain"), make_evidence()
    )
    assert len(result.errors) == 2


# --- Check 2: cited IDs come from this run


def test_a_claim_citing_an_id_no_tool_returned_is_rejected():
    claims = [{"code": "FAST_CHAIN", "evidence_ids": ["txn_invented"]}]
    assert "txn_invented" in errors_of(valid_payload(claims=claims))


def test_an_account_from_a_result_may_be_cited():
    claims = [{"code": "REASONS_CORROBORATED", "evidence_ids": ["2|B"]}]
    payload = valid_payload(verdict="raise", claims=claims)
    assert validate_decision(payload, make_evidence()).ok


# --- Check 3: no payments is not evidence


def test_promoted_ids_are_no_longer_accepted():
    payload = valid_payload(promoted_txn_ids=["txn_low"])
    assert "Unknown field(s): ['promoted_txn_ids']" in errors_of(payload)


def test_dont_raise_is_rejected_when_no_graph_has_payments():
    evidence = make_evidence(downstream=(), ranks={})
    payload = valid_payload(verdict="dont_raise", claims=[])
    assert "Use not_sure" in errors_of(payload, evidence)


def test_dont_raise_is_accepted_when_a_graph_has_payments():
    claims = [{"code": "REASONS_NOT_SUPPORTED", "evidence_ids": ["txn_a"]}]
    payload = valid_payload(verdict="dont_raise", claims=claims)
    assert validate_decision(payload, make_evidence()).ok


def test_raise_is_rejected_when_no_graph_has_payments():
    evidence = make_evidence(downstream=(), ranks={})
    payload = valid_payload(verdict="raise", claims=[])
    assert "raise needs a graph with payments" in errors_of(payload, evidence)


def test_raise_is_rejected_when_no_graph_was_fetched():
    evidence = RunEvidence(representative="txn_a", members=("txn_a",))
    evidence.record("get_alert", alert_result("txn_a"))
    payload = valid_payload(verdict="raise", claims=[])
    assert "raise needs a graph with payments" in errors_of(payload, evidence)


def test_raise_is_accepted_when_another_members_graph_has_payments():
    evidence = make_evidence(("txn_a", "txn_b"), downstream=(), ranks={})
    evidence.record("get_case_graph", graph_result("txn_b", ("txn_low",)))
    result = validate_decision(valid_payload(verdict="raise", claims=[]), evidence)
    assert result.ok
    # The representative's own graph is still empty, so the caveat and low confidence stay.
    assert [c["code"] for c in result.decision["claims"]] == ["NO_GRAPH_AVAILABLE"]
    assert result.decision["confidence"] == "low"


def test_not_sure_is_accepted_when_no_graph_has_payments():
    evidence = make_evidence(downstream=(), ranks={})
    assert validate_decision(valid_payload(verdict="not_sure", claims=[]), evidence).ok


def test_the_old_verdict_names_are_rejected():
    for old in ("escalate", "merge_into_case", "promote", "deprioritise", "need_more"):
        assert "verdict must be one of" in errors_of(valid_payload(verdict=old))


# --- Claim grounding


def test_neighbours_high_risk_must_cite_top_k_neighbours():
    claims = [{"code": "NEIGHBOURS_HIGH_RISK", "evidence_ids": ["txn_low"]}]
    assert "NEIGHBOURS_HIGH_RISK" in errors_of(valid_payload(claims=claims))


def test_neighbours_high_risk_without_neighbour_scores_is_rejected():
    evidence = make_evidence(ranks={})
    claims = [{"code": "NEIGHBOURS_HIGH_RISK", "evidence_ids": ["txn_high"]}]
    payload = valid_payload(verdict="raise", claims=claims)
    assert "NEIGHBOURS_HIGH_RISK" in errors_of(payload, evidence)


def test_fast_chain_must_cite_graph_payments():
    claims = [{"code": "FAST_CHAIN", "evidence_ids": ["txn_a"]}]
    assert "FAST_CHAIN" in errors_of(valid_payload(claims=claims))


def test_repeat_account_alerts_must_cite_related_alerts():
    claims = [{"code": "REPEAT_ACCOUNT_ALERTS", "evidence_ids": ["txn_b"]}]
    payload = valid_payload(verdict="raise", claims=claims)
    assert "REPEAT_ACCOUNT_ALERTS" in errors_of(payload)
    evidence = make_evidence(("txn_a", "txn_b"), related=related_result("txn_b"))
    assert validate_decision(payload, evidence).ok


def test_reasons_claims_need_reasons_and_a_graph():
    claims = [{"code": "REASONS_CORROBORATED", "evidence_ids": ["txn_a"]}]
    payload = valid_payload(verdict="raise", claims=claims)
    assert "needs the representative alert to have model reasons" in errors_of(
        payload, make_evidence(reasons=False)
    )
    assert "no payments" in errors_of(payload, make_evidence(downstream=(), ranks={}))


def test_corroborated_and_not_supported_contradict():
    claims = [
        {"code": "REASONS_CORROBORATED", "evidence_ids": ["txn_a"]},
        {"code": "REASONS_NOT_SUPPORTED", "evidence_ids": ["txn_a"]},
    ]
    payload = valid_payload(verdict="raise", claims=claims)
    assert "contradict" in errors_of(payload)


# --- Check 4: the note


def test_a_long_note_is_rejected():
    assert "limit" in errors_of(
        valid_payload(note="x" * (config.AGENT_NOTE_MAX_CHARS + 1))
    )


def test_a_note_at_the_limit_is_accepted():
    assert validate_decision(
        valid_payload(note="x" * config.AGENT_NOTE_MAX_CHARS), make_evidence()
    ).ok


def test_a_blank_note_becomes_null():
    assert (
        validate_decision(valid_payload(note="  "), make_evidence()).decision["note"]
        is None
    )


@pytest.mark.parametrize(
    "note",
    ["Gap of 15 minutes.", "57% of the alert amount.", "Paid at 10:57.", "txn_a0f7 again."],
)
def test_a_note_with_digits_is_rejected(note):
    assert "must not contain digits" in errors_of(valid_payload(note=note))


def test_a_note_without_digits_is_accepted():
    assert validate_decision(valid_payload(note="Chain looks fast."), make_evidence()).ok


@pytest.mark.parametrize(
    "note",
    ["Same sender in four top alerts.", "Two alerts form a chain.", "Fifteen payments.", "Within three hours."],
)
def test_a_note_with_a_spelled_out_number_is_rejected(note):
    assert "must not spell out numbers" in errors_of(valid_payload(note=note))


@pytest.mark.parametrize(
    "note",
    [
        "One of the accounts repeats.",
        "Multiple payments, often within hours.",
        "Someone's network looks tense and often stone-like.",
        "A canteen payment.",
    ],
)
def test_ordinary_words_are_not_mistaken_for_numbers(note):
    assert validate_decision(valid_payload(note=note), make_evidence()).ok


# --- Check 5: caveat claims match the flags


def test_a_false_caveat_claim_is_rejected():
    for code in ("NO_GRAPH_AVAILABLE", "TRUNCATED_WINDOW", "NO_REASONS_AVAILABLE"):
        claims = valid_payload()["claims"] + [{"code": code, "evidence_ids": ["txn_a"]}]
        assert code in errors_of(valid_payload(claims=claims))


def test_a_true_caveat_claim_is_accepted_once():
    evidence = make_evidence(truncated=True)
    claims = valid_payload()["claims"] + [
        {"code": "TRUNCATED_WINDOW", "evidence_ids": ["txn_a"]}
    ]
    result = validate_decision(valid_payload(claims=claims), evidence)
    assert [c["code"] for c in result.decision["claims"]].count("TRUNCATED_WINDOW") == 1


def test_missing_caveat_claims_are_added_from_the_flags():
    evidence = make_evidence(truncated=True, reasons=False)
    result = validate_decision(valid_payload(), evidence)
    codes = [c["code"] for c in result.decision["claims"]]
    assert "TRUNCATED_WINDOW" in codes and "NO_REASONS_AVAILABLE" in codes
    assert result.decision["claims"][-1]["evidence_ids"] == ["txn_a"]
    assert set(result.adjustments) >= {
        "added TRUNCATED_WINDOW",
        "added NO_REASONS_AVAILABLE",
    }


def test_no_graph_adds_its_claim():
    evidence = make_evidence(downstream=(), ranks={})
    payload = valid_payload(verdict="not_sure", claims=[])
    # claims may be empty in shape; the code adds the flag claim.
    result = validate_decision(payload, evidence)
    assert [c["code"] for c in result.decision["claims"]] == ["NO_GRAPH_AVAILABLE"]


def test_truncation_caps_confidence_at_medium():
    truncated = validate_decision(
        valid_payload(confidence="high"), make_evidence(truncated=True)
    )
    assert truncated.decision["confidence"] == "medium"
    assert "confidence lowered to medium" in truncated.adjustments

    low = validate_decision(
        valid_payload(confidence="low"), make_evidence(truncated=True)
    )
    assert low.decision["confidence"] == "low"


def test_no_graph_lowers_confidence_to_low():
    no_graph = validate_decision(
        valid_payload(
            verdict="not_sure", confidence="medium", claims=[]
        ),
        make_evidence(downstream=(), ranks={}),
    )
    assert no_graph.decision["confidence"] == "low"


def test_confidence_is_kept_when_nothing_limits_it():
    result = validate_decision(valid_payload(confidence="high"), make_evidence())
    assert result.decision["confidence"] == "high"
    assert result.adjustments == ()


def test_no_caveat_is_added_for_a_graph_that_was_never_fetched():
    evidence = make_evidence()
    evidence.graphs.clear()
    claims = [{"code": "REASONS_NOT_SUPPORTED", "evidence_ids": ["txn_a"]}]
    payload = valid_payload(verdict="dont_raise", claims=claims)
    assert "get_case_graph" in errors_of(payload, evidence)


def test_alerts_from_every_related_alerts_call_can_be_cited():
    evidence = make_evidence(("txn_a", "txn_b"), related=related_result("txn_x"))
    evidence.record("get_related_alerts", related_result("txn_y"))
    claims = [{"code": "REPEAT_ACCOUNT_ALERTS", "evidence_ids": ["txn_x", "txn_y"]}]
    payload = valid_payload(verdict="raise", claims=claims)
    assert validate_decision(payload, evidence).ok
    assert {a["txn_id"] for a in evidence.related["alerts"]} == {"txn_x", "txn_y"}
    assert evidence.related["ids"].count("2|B") == 1


def test_related_is_none_until_the_tool_runs():
    assert make_evidence().related is None
