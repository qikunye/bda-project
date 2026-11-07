import json
import re
from typing import get_args

import pytest

from src import config
from src.agent import brief, findings, sql
from src.agent.contracts import (
    DecisionRow,
    FindingCode,
    GapCode,
    InvestigateResult,
    MemberMoneyFlow,
    NextCheckCode,
    RunStats,
    TraceStep,
)
from src.agent.decisions import build_case_row, build_decision_rows, trace_key
from src.agent.cases import Case
from src.agent.sample_results import sample_decision_rows, sample_investigations

SAMPLES = sample_investigations()
OK_SAMPLES = {k: v for k, v in SAMPLES.items() if v["status"] == "ok"}


def tool_ids(result):
    found = set(result["alert"]["ids"])
    for step in result["trace"]:
        found.update(step["result"].get("ids", ()))
    for member in result["members"]:
        found.update(member["graph"]["ids"])
    return found


def all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_keys(item)


def test_samples_cover_every_situation():
    outcomes = {r["stats"]["validation"] for r in OK_SAMPLES.values()}
    assert outcomes == {"valid", "repaired", "fallback"}
    assert {"quiet_truncated", "no_graph", "fan_out_case", "code_only", "not_found"} <= set(SAMPLES)
    assert any(r["fallback"] for r in OK_SAMPLES.values())
    assert any(not r["findings"] for r in OK_SAMPLES.values())


def test_samples_between_them_show_every_finding_and_most_gap_codes():
    shown = {f["code"] for r in OK_SAMPLES.values() for f in r["findings"]}
    assert shown == set(get_args(FindingCode))
    gaps = {g["code"] for r in OK_SAMPLES.values() for g in r["gaps"]}
    # SHORT_HISTORY needs an alert with too few earlier days or a skipped activity check.
    assert gaps == set(get_args(GapCode)) - {"SHORT_HISTORY"}
    checks = {c["code"] for r in OK_SAMPLES.values() for c in r["next_checks"]}
    assert checks >= {"CHECK_ONWARD_ACCOUNTS", "CHECK_RETURN_FLOW", "RECHECK_AFTER_MORE_DATA"}


@pytest.mark.parametrize("name", SAMPLES)
def test_sample_matches_the_types(name):
    result = SAMPLES[name]
    assert set(result) == set(InvestigateResult.__annotations__)
    json.dumps(result)
    for step in result["trace"]:
        assert set(step) == set(TraceStep.__annotations__)
    for flow in result["money_flow"]:
        assert set(flow) == set(MemberMoneyFlow.__annotations__)
    if result["status"] == "ok":
        assert set(result["stats"]) == set(RunStats.__annotations__)
        assert result["stats"]["steps"] <= config.AGENT_MAX_STEPS
        assert result["fallback"] == result["stats"]["fallback"]
        assert result["summary"]
        assert "verdict" not in result and "confidence" not in result
        assert "reasons" not in result["alert"]


@pytest.mark.parametrize("name", SAMPLES)
def test_samples_hold_no_labels(name):
    assert not set(all_keys(SAMPLES[name])) & config.AGENT_FORBIDDEN_COLUMNS


@pytest.mark.parametrize("name", OK_SAMPLES)
def test_every_cited_id_came_from_a_tool(name):
    result, known = OK_SAMPLES[name], tool_ids(OK_SAMPLES[name])
    for finding in result["findings"]:
        assert finding["code"] in get_args(FindingCode)
        assert finding["evidence_ids"] and set(finding["evidence_ids"]) <= known
    if result["case_id"]:
        assert result["case_id"] == brief.case_id_for(result["txn_id"])
    sql.validate_transaction_id(result["txn_id"])


@pytest.mark.parametrize("name", OK_SAMPLES)
def test_gaps_match_the_tool_flags(name):
    result = OK_SAMPLES[name]
    codes = {g["code"] for g in result["gaps"]}
    lead = result["members"][0]["graph"]
    assert ("NO_GRAPH_AVAILABLE" in codes) is lead["no_graph"]
    assert ("TRUNCATED_WINDOW" in codes) is any(
        m["graph"]["window_truncated"] for m in result["members"][:1]
    )


@pytest.mark.parametrize("name", OK_SAMPLES)
def test_next_checks_follow_from_the_findings_and_gaps(name):
    result = OK_SAMPLES[name]
    present = {f["code"] for f in result["findings"]} | {g["code"] for g in result["gaps"]}
    for check in result["next_checks"]:
        assert check["code"] in get_args(NextCheckCode)
        assert brief.NEXT_CHECK_NEEDS[check["code"]] & present


@pytest.mark.parametrize("name", OK_SAMPLES)
def test_every_sample_lists_its_members_starting_with_the_representative(name):
    result = OK_SAMPLES[name]
    assert result["members"][0]["alert"]["txn_id"] == result["txn_id"]
    assert all(m["graph"]["txn_id"] == m["alert"]["txn_id"] for m in result["members"])
    assert [f["txn_id"] for f in result["money_flow"]] == [
        m["alert"]["txn_id"] for m in result["members"]
    ]


def test_a_case_sample_shows_every_member():
    result = SAMPLES["fan_out_case"]
    assert [m["alert"]["txn_id"] for m in result["members"]] == [
        "txn_samplefan",
        "txn_samplefanmember",
    ]
    assert result["case_id"] == "case_txn_samplefan"
    assert SAMPLES["pass_through"]["case_id"] is None


def test_totals_stay_per_currency():
    flow = SAMPLES["pass_through"]["money_flow"][0]["per_currency"]
    assert list(flow) == ["US Dollar"]
    assert flow["US Dollar"]["upstream_total"] == 200000.0


def test_sentences_take_numbers_from_tool_results():
    sentences = {f["code"]: f["sentence"] for f in SAMPLES["pass_through"]["findings"]}
    assert "90% of what came in" in sentences["PASS_THROUGH"]
    assert "25 minutes after the alert" in sentences["PASS_THROUGH"]
    fan = {f["code"]: f["sentence"] for f in SAMPLES["fan_out_case"]["findings"]}
    assert "6 different accounts (6 payments)" in fan["FAN_OUT"]


def test_every_finding_code_has_a_rule_and_a_label():
    assert set(findings.FINDING_CODES) == set(get_args(FindingCode))
    assert set(brief.GAP_CODES) == set(get_args(GapCode))
    assert set(brief.NEXT_CHECK_CODES) == set(get_args(NextCheckCode))


def test_summaries_carry_no_numbers():
    for name, result in OK_SAMPLES.items():
        assert not re.search(r"\d", result["summary"]), name


def test_a_fallback_sample_uses_the_code_only_summary():
    assert SAMPLES["fallback"]["fallback"] is True
    assert SAMPLES["fallback"]["summary"].startswith("The fixed rules found")
    assert SAMPLES["repaired"]["stats"]["repair_used"] is True


# --- Stored rows


def test_rows_have_one_full_representative_and_short_pointers():
    rows = build_decision_rows(
        SAMPLES["fan_out_case"],
        run_id="r1",
        created_at="2026-11-06T00:00:00+00:00",
        member_txn_ids=["txn_samplefanmember", "txn_samplefan"],
    )
    assert [r["role"] for r in rows] == ["representative", "member"]
    full, member = rows
    assert full["trace_key"] == trace_key("2022-09-14", "txn_samplefan")
    assert full["finding_codes"] == [f["code"] for f in SAMPLES["fan_out_case"]["findings"]]
    assert full["gap_codes"] == [] and full["next_check_codes"]
    assert full["summary"] == SAMPLES["fan_out_case"]["summary"]
    for field in ("finding_codes", "gap_codes", "next_check_codes", "summary", "model"):
        assert field not in member
    assert member["representative_txn_id"] == "txn_samplefan"
    assert member["case_id"] == full["case_id"]
    assert not {"verdict", "confidence", "claims", "note"} & set(full)


def test_a_case_row_lists_its_findings_and_members():
    case = Case("case_txn_samplefan", "2022-09-14", "txn_samplefan", ("txn_samplefan", "txn_samplefanmember"))
    row = build_case_row(case, SAMPLES["fan_out_case"], run_id="r1", created_at="t")
    assert row["finding_codes"] == [f["code"] for f in SAMPLES["fan_out_case"]["findings"]]
    assert row["size"] == 2 and row["fallback"] is False
    assert not {"verdict", "confidence"} & set(row)


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
