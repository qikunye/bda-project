"""Tests for the added evaluation pieces: rank bands, cost and latency, repeatability, the deep S1
runner and the summary. All with fakes; nothing reads labels or calls AWS."""

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import run_evaluation, run_s1_deep, summarise_evaluation
from src.evaluation import metrics, repeatability
from src.evaluation.runs import CaseOutcome, load_run


def case(cid, members=("t",), codes=(), **kw):
    return CaseOutcome(cid, tuple(members), frozenset(codes), **kw)


def test_band_by_rank_uses_the_representative_rank_and_skips_unranked_cases():
    cases = [
        case("a", rank_day=1),
        case("b", rank_day=100),
        case("c", rank_day=101),
        case("d", rank_day=480),
        case("e"),
    ]
    assert metrics.band_by_rank(cases) == {
        "a": "1-100",
        "b": "1-100",
        "c": "101-250",
        "d": "251+",
    }


def test_bands_for_falls_back_to_thirds_when_ranks_are_missing():
    ranked = [case("a", rank_day=5), case("b", rank_day=300)]
    assert run_evaluation.bands_for(ranked) == {"a": "1-100", "b": "251+"}
    unranked = [case("a"), case("b"), case("c")]
    assert run_evaluation.bands_for(unranked) == {
        "a": "top",
        "b": "middle",
        "c": "bottom",
    }


def test_cost_latency_estimates_spend_from_tokens():
    runs = [
        case(
            "a",
            stats={
                "tokens_in": 1_000_000,
                "tokens_out": 0,
                "latency_ms": 10_000,
                "steps": 4,
            },
        ),
        case(
            "b",
            stats={
                "tokens_in": 0,
                "tokens_out": 1_000_000,
                "latency_ms": 30_000,
                "steps": 6,
            },
        ),
    ]
    out = metrics.cost_latency(runs, (3.0, 15.0))
    assert (
        out["estimated_spend_usd_total"] == 18.0
        and out["estimated_spend_usd_per_case"] == 9.0
    )
    assert (
        out["latency_s_per_case"]["median"] == 20.0
        and out["steps_per_case"]["max"] == 6
    )


def test_repeatability_counts_agreement_and_unstable_codes():
    run1 = [
        case("a", codes=["FAN_OUT"], validation="valid", summary="x"),
        case("b", codes=[], validation="valid"),
    ]
    run2 = [
        case("a", codes=["FAN_OUT", "FAN_IN"], validation="valid", summary="x"),
        case("b", codes=[], validation="repaired"),
    ]
    run3 = [case("a", codes=["FAN_OUT"], validation="valid", summary="y"), case("c")]
    out = repeatability.compare([run1, run2, run3])
    assert out["cases_compared"] == 1 and out["cases_missing_from_some_run"] == [
        "b",
        "c",
    ]
    assert out["same_findings_every_run"]["positives"] == 0
    assert out["same_validation_outcome_every_run"]["positives"] == 1
    assert out["finding_codes_not_in_every_run"] == {"FAN_IN": 1}
    assert 0 < out["mean_pairwise_finding_jaccard"] < 1
    with pytest.raises(ValueError):
        repeatability.compare([run1])


def test_load_run_reads_ranks_from_deep_s1_and_from_s2_alerts(tmp_path):
    deep = {
        "cases": [
            {
                "case_id": "a",
                "member_txn_ids": ["t"],
                "representative_rank_day": 312,
                "findings": [],
            }
        ]
    }
    s2 = {
        "results": [
            {
                "case_id": "b",
                "result": {
                    "alert": {"risk_rank_day": 7},
                    "members": [{"alert": {"txn_id": "u"}}],
                },
            }
        ]
    }
    (tmp_path / "d.json").write_text(json.dumps(deep))
    (tmp_path / "s.json").write_text(json.dumps(s2))
    assert load_run(tmp_path / "d.json")[0].rank_day == 312
    assert load_run(tmp_path / "s.json")[0].rank_day == 7


def test_deep_s1_runs_cases_concurrently_and_keeps_queue_order(monkeypatch):
    frame = pd.DataFrame(
        {
            "transaction_id": ["txn_a", "txn_b", "txn_c"],
            "risk_rank_day": [1, 2, 3],
        }
    )
    cases = [
        SimpleNamespace(
            case_id="ca", representative_txn_id="txn_a", member_txn_ids=("txn_a",)
        ),
        SimpleNamespace(
            case_id="cb",
            representative_txn_id="txn_b",
            member_txn_ids=("txn_b", "txn_c"),
        ),
    ]
    monkeypatch.setattr(run_s1_deep.tools, "load_day_queue", lambda data, day, n: frame)
    monkeypatch.setattr(
        run_s1_deep, "queue_alerts_from_frame", lambda f: list(f["transaction_id"])
    )
    monkeypatch.setattr(
        run_s1_deep, "group_into_cases", lambda alerts, day, window_h: cases
    )
    seen_settings = []

    def fake_brief(data, c, settings):
        seen_settings.append(settings.as_of)
        if c.case_id == "cb":
            return {
                "summary": "s",
                "findings": [{"code": "FAN_OUT"}],
                "gaps": [],
                "next_checks": [],
            }
        return {"summary": "s", "findings": [], "gaps": [], "next_checks": []}

    monkeypatch.setattr(run_s1_deep, "brief_case", fake_brief)
    out = run_s1_deep.run_day(object(), "2022-09-14", 3, concurrency=2)
    assert [c["case_id"] for c in out["cases"]] == ["ca", "cb"]
    assert out["cases"][1]["representative_rank_day"] == 2 and out["failures"] == []
    assert set(seen_settings) == {
        "2022-09-14 23:59:59"
    }  # Same end-of-day cutoff as the batch.


def test_deep_s1_keeps_going_when_one_case_fails(monkeypatch):
    frame = pd.DataFrame(
        {"transaction_id": ["txn_a", "txn_b"], "risk_rank_day": [1, 2]}
    )
    cases = [
        SimpleNamespace(
            case_id="ca", representative_txn_id="txn_a", member_txn_ids=("txn_a",)
        ),
        SimpleNamespace(
            case_id="cb", representative_txn_id="txn_b", member_txn_ids=("txn_b",)
        ),
    ]
    monkeypatch.setattr(run_s1_deep.tools, "load_day_queue", lambda data, day, n: frame)
    monkeypatch.setattr(run_s1_deep, "queue_alerts_from_frame", lambda f: [])
    monkeypatch.setattr(
        run_s1_deep, "group_into_cases", lambda alerts, day, window_h: cases
    )

    def fake_brief(data, c, settings):
        if c.case_id == "ca":
            raise RuntimeError("athena")
        return {"summary": "s", "findings": [], "gaps": [], "next_checks": []}

    monkeypatch.setattr(run_s1_deep, "brief_case", fake_brief)
    out = run_s1_deep.run_day(object(), "2022-09-14", 2, concurrency=2)
    assert [c["case_id"] for c in out["cases"]] == ["cb"] and out["failures"][0][
        "case_id"
    ] == "ca"


def test_summary_picks_examples_without_labels_and_renders_a_day(tmp_path):
    def result(cid, codes, validation):
        return {
            "case_id": cid,
            "result": {
                "case_id": cid,
                "summary": "s",
                "members": [{"alert": {"txn_id": cid + "_t"}}],
                "findings": [{"code": c, "sentence": c.lower()} for c in codes],
                "gaps": [],
                "next_checks": [],
                "observations": [],
                "stats": {"validation": validation},
            },
        }

    s2 = {
        "results": [
            result("plain", [], "valid"),
            result("rich", ["FAN_OUT", "FAN_IN"], "valid"),
            result("fb", [], "fallback"),
        ]
    }
    path = tmp_path / "s2.json"
    path.write_text(json.dumps(s2))
    names = [
        name.split(" ")[0] for name, _ in summarise_evaluation.pick_examples(str(path))
    ]
    assert names == ["Good", "Ordinary", "Fallback"]
    report = {
        "day": "2022-09-14",
        "labels_found": {"alerts": 3, "with_label": 3, "positives": 1},
        "reliability_s2": {
            "validation": {"valid": 2},
            "fallback": {"rate": 0.0, "n": 3, "positives": 0, "ci95": [0, 0.5]},
        },
        "s1_code_rates": {
            "FAN_OUT": {
                "with": {"rate": 0.5, "n": 2, "positives": 1, "ci95": [0.1, 0.9]},
                "without": {"rate": None},
            }
        },
        "s2_code_rates": {},
    }
    text = "\n".join(summarise_evaluation.day_section(report))
    assert "## 2022-09-14" in text and "FAN_OUT" in text and "50.0% (1/2" in text


def test_run_evaluation_end_to_end_with_fakes(tmp_path, monkeypatch, capsys):
    """The whole report with the deeper S1 list, fake labels and a fake patterns file."""

    def brief(cid, members, codes, **extra):
        return {
            "case_id": cid,
            "member_txn_ids": members,
            "findings": [{"code": c} for c in codes],
            **extra,
        }

    s1 = {"cases": [brief("ca", ["txn_a"], ["FAN_OUT"]), brief("cb", ["txn_b"], [])]}
    s2 = {
        "results": [
            {
                "case_id": "ca",
                "result": {
                    "case_id": "ca",
                    "alert": {"risk_rank_day": 1},
                    "members": [{"alert": {"txn_id": "txn_a"}}],
                    "findings": [{"code": "FAN_OUT"}],
                    "stats": {
                        "validation": "valid",
                        "tokens_in": 10000,
                        "tokens_out": 1000,
                        "latency_ms": 12000,
                    },
                },
            },
            {
                "case_id": "cb",
                "result": {
                    "case_id": "cb",
                    "alert": {"risk_rank_day": 2},
                    "members": [{"alert": {"txn_id": "txn_b"}}],
                    "findings": [],
                    "stats": {
                        "validation": "fallback",
                        "tokens_in": 5000,
                        "tokens_out": 500,
                        "latency_ms": 20000,
                    },
                },
            },
        ]
    }
    deep = {
        "cases": [
            brief("ca", ["txn_a"], ["FAN_OUT"], representative_rank_day=1),
            brief("cb", ["txn_b"], [], representative_rank_day=2),
            brief(
                "cc", ["txn_c"], ["REPEAT_ACCOUNT_ALERTS"], representative_rank_day=300
            ),
        ]
    }
    for name, body in (("s1", s1), ("s2", s2), ("deep", deep)):
        (tmp_path / f"{name}.json").write_text(json.dumps(body))

    def fake_labels(run, ids):
        rows = [
            {
                "transaction_id": t,
                "source_account_key": "1|A",
                "target_account_key": "2|B",
                "transaction_timestamp": "2022-09-14 10:05:00",
            }
            for t in ids
        ]
        return {"txn_a": True, "txn_b": False, "txn_c": False}, rows

    class FakeS3:
        def get_object(self, **kw):
            return {"Body": SimpleNamespace(read=lambda: b"")}

    monkeypatch.setattr(run_evaluation, "fetch_labels", fake_labels)
    monkeypatch.setattr(run_evaluation, "athena_runner", lambda: None)
    monkeypatch.setattr(run_evaluation.boto3, "client", lambda *a, **k: FakeS3())
    out = tmp_path / "eval.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_evaluation",
            "--day",
            "2022-09-14",
            "--s1",
            str(tmp_path / "s1.json"),
            "--s2",
            str(tmp_path / "s2.json"),
            "--s1-deep",
            str(tmp_path / "deep.json"),
            "--out",
            str(out),
            "--confirm-frozen",
        ],
    )
    assert run_evaluation.main() == 0
    report = json.loads(out.read_text())
    assert report["labels_found"] == {"alerts": 3, "with_label": 3, "positives": 1}
    assert report["cost_latency_s2"]["cases"] == 2
    deep_bands = report["s1_deep"]["bq1_derived_by_rank_band"]["REPEAT_ACCOUNT_ALERTS"]
    assert set(deep_bands) == {"1-100", "251+"}
    assert report["s1_deep"]["code_rates"]["FAN_OUT"]["with"]["positives"] == 1
    md = tmp_path / "summary.md"
    monkeypatch.setattr(
        "sys.argv",
        [
            "summarise",
            "--eval",
            str(out),
            "--s2",
            str(tmp_path / "s2.json"),
            "--out",
            str(md),
        ],
    )
    assert summarise_evaluation.main() == 0
    text = md.read_text()
    assert "## 2022-09-14" in text and "Example briefs" in text and "Fallback" in text


def test_same_rules_agree_flags_a_changed_case():
    a = CaseOutcome("c1", ("t1",), frozenset({"FAN_OUT", "NEIGHBOURS_HIGH_RISK"}))
    b = CaseOutcome("c2", ("t2",), frozenset())
    deep = [
        CaseOutcome("c1", ("t1",), frozenset({"FAN_OUT"})),
        CaseOutcome("c2", ("t2",), frozenset()),
        CaseOutcome("c3", ("t3",), frozenset({"FAN_IN"})),
    ]
    out = metrics.same_rules_agree([a, b], deep)
    assert out == {"cases_in_both": 2, "differ": 1, "first_differ": ["c1"]}
    assert metrics.same_rules_agree([b], deep)["differ"] == 0
