import json
import re
from typing import get_args

import pytest

from src import config
from src.agent import claims, sql
from src.agent.contracts import (
    ClaimCode,
    Confidence,
    Decision,
    DecisionRow,
    InvestigateResult,
    RunStats,
    TraceStep,
    Verdict,
)
from src.agent.decisions import build_decision_rows, trace_key
from src.agent.sample_results import sample_decision_rows, sample_investigations

SAMPLES = sample_investigations()
OK_SAMPLES = {k: v for k, v in SAMPLES.items() if v["status"] == "ok"}


def tool_ids(result):
    found = set(result["alert"]["ids"])
    for name in ("graph", "neighbours", "related"):
        if result[name] is not None:
            found.update(result[name]["ids"])
    return found


def all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_keys(item)


def test_samples_cover_every_verdict_and_situation():
    verdicts = {r["decision"]["verdict"] for r in OK_SAMPLES.values()}
    assert verdicts == set(get_args(Verdict))
    outcomes = {r["stats"]["validation"] for r in OK_SAMPLES.values()}
    assert outcomes == {"valid", "repaired", "fallback"}
    assert {"no_reasons", "no_graph", "truncated", "not_found"} <= set(SAMPLES)


@pytest.mark.parametrize("name", SAMPLES)
def test_sample_matches_the_types(name):
    result = SAMPLES[name]
    assert set(result) == set(InvestigateResult.__annotations__)
    json.dumps(result)
    for step in result["trace"]:
        assert set(step) == set(TraceStep.__annotations__)
    if result["status"] == "ok":
        assert set(result["decision"]) == set(Decision.__annotations__)
        assert set(result["stats"]) == set(RunStats.__annotations__)
        assert result["decision"]["verdict"] in get_args(Verdict)
        assert result["decision"]["confidence"] in get_args(Confidence)
        assert result["stats"]["steps"] <= 5


@pytest.mark.parametrize("name", SAMPLES)
def test_samples_hold_no_labels(name):
    assert not set(all_keys(SAMPLES[name])) & config.AGENT_FORBIDDEN_COLUMNS


@pytest.mark.parametrize("name", OK_SAMPLES)
def test_every_cited_id_came_from_a_tool(name):
    result = OK_SAMPLES[name]
    decision, known = result["decision"], tool_ids(result)
    for claim in decision["claims"]:
        assert claim["code"] in get_args(ClaimCode)
        assert set(claim["evidence_ids"]) <= known
    if decision["case_id"]:
        assert decision["case_id"] == claims.case_id_for(result["txn_id"])
    sql.validate_transaction_id(result["txn_id"])


@pytest.mark.parametrize("name", OK_SAMPLES)
def test_flag_claims_match_the_tool_flags(name):
    result = OK_SAMPLES[name]
    codes = {c["code"] for c in result["decision"]["claims"]}
    if "NO_GRAPH_AVAILABLE" in codes:
        assert result["graph"]["no_graph"]
    if "TRUNCATED_WINDOW" in codes:
        assert result["graph"]["window_truncated"]
    if "NO_REASONS_AVAILABLE" in codes:
        assert result["alert"]["reasons"] is None


def test_a_no_graph_sample_is_never_dont_raise():
    assert SAMPLES["no_graph"]["decision"]["verdict"] == "not_sure"


@pytest.mark.parametrize("name", OK_SAMPLES)
def test_every_sample_lists_its_members_starting_with_the_representative(name):
    result = OK_SAMPLES[name]
    assert result["members"][0]["alert"]["txn_id"] == result["txn_id"]
    assert all(m["graph"]["txn_id"] == m["alert"]["txn_id"] for m in result["members"])


def test_a_case_sample_shows_every_member():
    members = SAMPLES["raise_case"]["members"]
    assert [m["alert"]["txn_id"] for m in members] == [
        "txn_samplemerge",
        "txn_samplemember1",
    ]


def test_totals_stay_per_currency():
    totals = SAMPLES["truncated"]["graph"]["per_currency_totals"]["upstream"]
    assert totals == {"Euro": 90000.0, "US Dollar": 90000.0}


def test_every_claim_code_has_a_sentence():
    assert set(claims._RENDERERS) == set(get_args(ClaimCode))


def test_sentences_take_numbers_from_tool_results():
    result = SAMPLES["raise_case"]
    sentences = [c["sentence"] for c in result["rendered_claims"]]
    assert (
        sentences[0]
        == f"Neighbours in the day's top {config.AGENT_TOP_K}: 2 of 2 scored."
    )
    assert (
        sentences[1]
        == f"Other top {config.AGENT_TOP_K} alerts on the same accounts within 48 h: 1."
    )
    assert "12 minutes" in SAMPLES["raise"]["rendered_claims"][1]["sentence"]


def test_a_fast_chain_gap_can_come_from_another_members_graph():
    graph = SAMPLES["raise"]["graph"]
    edge = graph["downstream"][0]
    claim = {"code": "FAST_CHAIN", "evidence_ids": [edge["txn_id"]]}
    sentence = claims.render_claim(claim, member_graphs=[graph])["sentence"]
    assert f"{edge['gap_minutes']} minutes" in sentence


def test_unscored_neighbours_are_not_counted():
    claim = {"code": "NEIGHBOURS_HIGH_RISK", "evidence_ids": []}
    neighbours = SAMPLES["dont_raise"]["neighbours"]
    sentence = claims.render_claim(claim, neighbours=neighbours)["sentence"]
    assert sentence.endswith("0 of 0 scored.")


@pytest.mark.parametrize("code", get_args(ClaimCode))
def test_sentence_has_no_numbers_when_no_tool_ran(code):
    rendered = claims.render_claim({"code": code, "evidence_ids": ["txn_a1"]})
    assert not re.search(r"\d", rendered["sentence"])


def test_sentences_never_use_the_llm_note():
    result = SAMPLES["not_sure"]
    note = result["decision"]["note"]
    assert note and all(note not in c["sentence"] for c in result["rendered_claims"])


def test_rows_have_one_full_representative_and_short_pointers():
    rows = build_decision_rows(
        SAMPLES["raise_case"],
        run_id="r1",
        created_at="2026-11-06T00:00:00+00:00",
        member_txn_ids=["txn_samplemember1", "txn_samplemerge"],
    )
    assert [r["role"] for r in rows] == ["representative", "member"]
    full, member = rows
    assert full["trace_key"] == trace_key("2022-09-14", "txn_samplemerge")
    assert full["claims"]
    assert "claims" not in member
    assert member["representative_txn_id"] == "txn_samplemerge"
    assert member["case_id"] == full["case_id"]
    assert member["verdict"] == full["verdict"]


def test_not_found_has_no_rows():
    with pytest.raises(ValueError):
        build_decision_rows(SAMPLES["not_found"], run_id="r1", created_at="t")


def test_stored_rows_suit_dynamodb():
    def floats(value):
        if isinstance(value, float):
            return True
        if isinstance(value, dict):
            return any(floats(v) for v in value.values())
        if isinstance(value, list):
            return any(floats(v) for v in value)
        return False

    rows = sample_decision_rows()
    assert len({r["transaction_id"] for r in rows}) == len(rows)
    for row in rows:
        assert set(row) <= set(DecisionRow.__annotations__)
        assert not floats(row)
        assert not set(all_keys(row)) & config.AGENT_FORBIDDEN_COLUMNS
