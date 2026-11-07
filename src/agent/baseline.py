"""Builds the brief with fixed rules and no LLM, as a baseline to compare against.
It is also the fallback when the LLM fails."""

import time
from dataclasses import dataclass, replace

from src import config
from src.agent import brief, findings, tools
from src.agent.cases import Case, group_into_cases, queue_alerts_from_frame
from src.agent.contracts import (
    CaseMember,
    InvestigateResult,
    RunStats,
    TraceStep,
)
from src.agent.data import AgentData
from src.agent.evidence import RunEvidence

MODEL_NAME = "deterministic-s1"


@dataclass(frozen=True)
class Settings:
    window_h: int = config.AGENT_WINDOW_H_DEFAULT
    top_k: int = config.AGENT_TOP_K
    # Hide data after this time. A daily run knows only what has happened by the end of its day.
    as_of: str | None = None


def end_of_day(day: str) -> str:
    return f"{day} 23:59:59"


def brief_case(
    data: AgentData, case: Case, settings: Settings = Settings()
) -> InvestigateResult:
    """Run every tool for every member, then apply the finding rules. The representative is first."""

    started = time.monotonic()
    trace: list[TraceStep] = []
    evidence = RunEvidence(
        representative=case.representative_txn_id,
        members=case.member_txn_ids,
        top_k=settings.top_k,
        txn_dates={m: case.txn_date for m in case.member_txn_ids},
    )

    def run(tool_name: str, inputs: dict, call):
        tool_started = time.monotonic()
        result = call()
        trace.append(
            TraceStep(
                step=len(trace),
                actor="code",
                tool=tool_name,
                inputs=inputs,
                result=result,
                latency_ms=int((time.monotonic() - tool_started) * 1000),
            )
        )
        evidence.record(tool_name, result)
        return result

    members: list[CaseMember] = []
    for txn_id in case.member_txn_ids:
        alert = run(
            "get_alert",
            {"txn_id": txn_id, "txn_date": case.txn_date},
            lambda: tools.get_alert(data, txn_id, case.txn_date, top_k=settings.top_k),
        )
        if not alert.get("found"):
            raise ValueError(f"Queue alert {txn_id} was not found on {case.txn_date}.")
        graph = run(
            "get_case_graph",
            {"txn_id": txn_id, "window_h": settings.window_h},
            lambda: tools.get_case_graph(
                data, alert, settings.window_h, as_of=settings.as_of
            ),
        )
        activities = [
            run(
                "get_account_activity",
                {"txn_id": txn_id, "side": side, "window_h": settings.window_h},
                lambda side=side: tools.get_account_activity(
                    data, alert, side, settings.window_h, as_of=settings.as_of
                ),
            )
            for side in ("sender", "receiver")
        ]
        # Score every payment a graph or activity result lists, as the finding rule counts them all.
        listed = [
            p
            for p in [
                *graph["upstream"],
                *graph["downstream"],
                *(p for a in activities for p in (*a["incoming"], *a["outgoing"])),
            ]
        ]
        unique = list({p["txn_id"]: p for p in listed}.values())[
            : config.AGENT_MAX_NEIGHBOUR_IDS
        ]
        run(
            "get_neighbour_scores",
            {"txn_ids": [p["txn_id"] for p in unique]},
            lambda: tools.get_neighbour_scores(
                data,
                [p["txn_id"] for p in unique],
                [p["txn_date"] for p in unique],
                top_k=settings.top_k,
            ),
        )
        members.append(CaseMember(alert=alert, graph=graph))

    lead = members[0]["alert"]
    accounts = [lead["sender"], lead["receiver"]]
    run(
        "get_related_alerts",
        {"accounts": accounts},
        lambda: tools.get_related_alerts(
            data,
            accounts,
            lead["ts"],
            settings.window_h,
            exclude_txn_ids=tuple(case.member_txn_ids),
            top_k=settings.top_k,
            as_of=settings.as_of,
        ),
    )

    return brief_from_evidence(
        case,
        settings.window_h,
        evidence,
        members,
        trace,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


def brief_from_evidence(
    case: Case,
    window_h: int,
    evidence: RunEvidence,
    members: list[CaseMember],
    trace: list[TraceStep],
    *,
    latency_ms: int = 0,
) -> InvestigateResult:
    """Apply the finding rules to what the tools returned and assemble the code-only brief."""

    instances = findings.detect(evidence)
    found = findings.s1_findings(instances)
    finding_codes = [code for code, _ in found]
    gaps = brief.compute_gaps(evidence)
    checks = brief.allowed_next_checks(set(finding_codes), {g["code"] for g in gaps})
    lead = members[0]["alert"]

    return InvestigateResult(
        status="ok",
        txn_id=case.representative_txn_id,
        txn_date=case.txn_date,
        window_h=window_h,
        alert=lead,
        case_id=brief.case_id_for(case.representative_txn_id)
        if len(case.member_txn_ids) > 1
        else None,
        summary=brief.template_summary(finding_codes),
        findings=brief.render_findings(found, instances),
        gaps=gaps,
        next_checks=brief.render_next_checks(checks),
        observations=[],  # Only the LLM writes these.
        money_flow=brief.build_money_flow(members),
        members=members,
        fallback=False,
        stats=RunStats(
            model=MODEL_NAME,
            steps=0,
            tokens_in=0,
            tokens_out=0,
            latency_ms=latency_ms,
            validation="valid",
            repair_used=False,
            fallback=False,
            observations_dropped=0,
        ),
        trace=trace,
    )


def brief_day(
    data: AgentData,
    txn_date: str,
    *,
    top_n: int = config.AGENT_BATCH_TOP_N,
    settings: Settings = Settings(),
) -> list[tuple[Case, InvestigateResult]]:
    """Group the day's top N alerts into cases and build the code-only brief for each, as of the end of that day."""

    settings = replace(settings, as_of=end_of_day(txn_date))
    frame = tools.load_day_queue(data, txn_date, top_n)
    cases = group_into_cases(
        queue_alerts_from_frame(frame), txn_date, window_h=settings.window_h
    )
    return [(case, brief_case(data, case, settings)) for case in cases]
