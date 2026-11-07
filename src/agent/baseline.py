"""S1: the deterministic baseline and LLM fallback. Same tools, fixed rules (PLAN.md 8, D26).

Rules, first match wins:
1. raise: the case has at least the minimum number of high-ranked transactions. Those are its
   members plus neighbours inside the day's top K.
2. not_sure: anything else, including no payments to check, because no evidence is not evidence
   of innocence.

S1 has no rule for dont_raise, so only the LLM can move an alert down.
"""

import time
from dataclasses import dataclass, replace

from src import config
from src.agent import tools
from src.agent.cases import Case, group_into_cases, queue_alerts_from_frame
from src.agent.claims import case_id_for, render_claims
from src.agent.contracts import (
    Alert,
    CaseGraph,
    CaseMember,
    Claim,
    Decision,
    InvestigateResult,
    NeighbourScore,
    NeighbourScores,
    RelatedAlerts,
    RunStats,
    TraceStep,
)
from src.agent.data import AgentData

MODEL_NAME = "deterministic-s1"


@dataclass(frozen=True)
class Settings:
    window_h: int = config.AGENT_WINDOW_H_DEFAULT
    top_k: int = config.AGENT_TOP_K
    escalate_min_high_ranked: int = config.AGENT_S1_ESCALATE_MIN_HIGH_RANKED
    # Hide data after this time. A daily run knows only what has happened by the end of its day.
    as_of: str | None = None


def end_of_day(day: str) -> str:
    return f"{day} 23:59:59"


@dataclass(frozen=True)
class MemberEvidence:
    alert: Alert
    graph: CaseGraph
    neighbours: NeighbourScores


def _merged_neighbours(evidence: list[MemberEvidence]) -> list[NeighbourScore]:
    merged: dict[str, NeighbourScore] = {}
    for member in evidence:
        for neighbour in member.neighbours["neighbours"]:
            merged.setdefault(neighbour["txn_id"], neighbour)
    return list(merged.values())


def _by_rank(neighbours: list[NeighbourScore]) -> list[str]:
    ordered = sorted(neighbours, key=lambda n: (n["risk_rank_day"], n["txn_id"]))
    return [n["txn_id"] for n in ordered]


def decide_case(
    case: Case,
    evidence: list[MemberEvidence],
    related: RelatedAlerts,
    settings: Settings = Settings(),
) -> Decision:
    """Apply the fixed rules. No AWS, and the same input always gives the same decision."""

    representative = case.representative_txn_id
    members = set(case.member_txn_ids)
    neighbours = [n for n in _merged_neighbours(evidence) if n["txn_id"] not in members]
    high = [n for n in neighbours if n["in_day_top_k"]]
    high_ranked = len(case.member_txn_ids) + len(high)
    no_graph = all(member.graph["no_graph"] for member in evidence)
    truncated = any(member.graph["window_truncated"] for member in evidence)

    claims: list[Claim] = []
    cited_members = sorted(
        ({a["txn_id"] for a in related["alerts"]} & members) - {representative}
    )
    if cited_members:
        claims.append(Claim(code="REPEAT_ACCOUNT_ALERTS", evidence_ids=cited_members))
    if high:
        claims.append(Claim(code="NEIGHBOURS_HIGH_RISK", evidence_ids=_by_rank(high)))

    note = None
    if high_ranked >= settings.escalate_min_high_ranked:
        verdict, confidence = "raise", "medium"
    elif no_graph:
        verdict, confidence = "not_sure", "low"
        claims.append(Claim(code="NO_GRAPH_AVAILABLE", evidence_ids=[representative]))
    else:
        verdict, confidence = "not_sure", "low"
        note = "No fixed rule applied to this case."

    if truncated:
        claims.append(Claim(code="TRUNCATED_WINDOW", evidence_ids=[representative]))
    if evidence[0].alert["reasons"] is None:
        claims.append(Claim(code="NO_REASONS_AVAILABLE", evidence_ids=[representative]))

    return Decision(
        verdict=verdict,
        confidence=confidence,
        case_id=case_id_for(representative) if len(members) > 1 else None,
        claims=claims,
        note=note,
    )


def triage_case(
    data: AgentData, case: Case, settings: Settings = Settings()
) -> InvestigateResult:
    """Run the tools for every member, then decide. The representative comes first in evidence."""

    started = time.monotonic()
    trace: list[TraceStep] = []

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
        return result

    evidence: list[MemberEvidence] = []
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
        edges = graph["upstream"] + graph["downstream"]
        neighbours = run(
            "get_neighbour_scores",
            {"txn_ids": [e["txn_id"] for e in edges]},
            lambda: tools.get_neighbour_scores(
                data,
                [e["txn_id"] for e in edges],
                [e["txn_date"] for e in edges],
                top_k=settings.top_k,
            ),
        )
        evidence.append(MemberEvidence(alert, graph, neighbours))

    lead = evidence[0]
    related = run(
        "get_related_alerts",
        {"accounts": [lead.alert["sender"], lead.alert["receiver"]]},
        lambda: tools.get_related_alerts(
            data,
            [lead.alert["sender"], lead.alert["receiver"]],
            lead.alert["ts"],
            settings.window_h,
            exclude_txn_ids=(case.representative_txn_id,),
            top_k=settings.top_k,
            as_of=settings.as_of,
        ),
    )

    decision = decide_case(case, evidence, related, settings)
    merged = _merged_neighbours(evidence)
    all_neighbours = NeighbourScores(
        neighbours=merged, ids=[n["txn_id"] for n in merged]
    )
    return InvestigateResult(
        status="ok",
        txn_id=case.representative_txn_id,
        txn_date=case.txn_date,
        window_h=settings.window_h,
        alert=lead.alert,
        decision=decision,
        rendered_claims=render_claims(
            decision,
            graph=lead.graph,
            neighbours=all_neighbours,
            related=related,
            member_graphs=[member.graph for member in evidence],
        ),
        graph=lead.graph,
        neighbours=all_neighbours,
        related=related,
        members=[
            CaseMember(alert=member.alert, graph=member.graph) for member in evidence
        ],
        stats=RunStats(
            model=MODEL_NAME,
            steps=0,
            tokens_in=0,
            tokens_out=0,
            latency_ms=int((time.monotonic() - started) * 1000),
            validation="valid",
            repair_used=False,
            fallback=False,
        ),
        trace=trace,
    )


def triage_day(
    data: AgentData,
    txn_date: str,
    *,
    top_n: int = config.AGENT_BATCH_TOP_N,
    settings: Settings = Settings(),
) -> list[tuple[Case, InvestigateResult]]:
    """Group the day's top N alerts into cases and run S1 on each, as of the end of that day."""

    settings = replace(settings, as_of=end_of_day(txn_date))
    frame = tools.load_day_queue(data, txn_date, top_n)
    cases = group_into_cases(
        queue_alerts_from_frame(frame), txn_date, window_h=settings.window_h
    )
    return [(case, triage_case(data, case, settings)) for case in cases]
