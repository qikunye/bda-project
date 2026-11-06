"""Made-up agent results, so Streamlit can be built before the agent exists (PLAN.md 19).

Everything here is invented: the IDs, numbers and verdicts are not from the dataset, and the model
is "sample". Do not use these in results or show them as real.
"""

from datetime import datetime, timedelta

from src import config
from src.agent.claims import case_id_for, render_claims
from src.agent.contracts import (
    Alert,
    CaseGraph,
    Claim,
    Decision,
    DecisionRow,
    Edge,
    InvestigateResult,
    NeighbourScore,
    NeighbourScores,
    Reason,
    RelatedAlert,
    RelatedAlerts,
    RunStats,
    TraceStep,
)
from src.agent.decisions import build_decision_rows

SAMPLE_DAY = "2022-09-14"
SAMPLE_RUN_ID = "sample-run"
SAMPLE_CREATED_AT = "2026-11-06T00:00:00+00:00"
SENDER = "100|SAMPLE1"
RECEIVER = "200|SAMPLE2"
WINDOW_H = 48

_REASONS = [
    Reason(
        feature="payment_format",
        sentence="↑ Paid by ACH",
        direction="up",
        contribution=3.1,
    ),
    Reason(
        feature="pair_prior_cnt",
        sentence="↑ First payment between these two accounts",
        direction="up",
        contribution=2.2,
    ),
    Reason(
        feature="hour_of_day",
        sentence="↓ Paid at 14:00",
        direction="down",
        contribution=-0.4,
    ),
]


def _alert(
    txn_id: str, rank_day: int, *, ts: str = "2022-09-14 10:00:00", reasons: bool = True
) -> Alert:
    return Alert(
        found=True,
        txn_id=txn_id,
        txn_date=ts[:10],
        ts=ts,
        sender=SENDER,
        receiver=RECEIVER,
        amount=250000.0,
        currency="US Dollar",
        payment_format="ACH",
        risk_score=0.75,
        risk_rank_global=rank_day * 10,
        risk_rank_day=rank_day,
        in_queue=True,
        in_day_top_k=rank_day <= config.AGENT_TOP_K,
        reasons=list(_REASONS) if reasons else None,
        ids=[txn_id, SENDER, RECEIVER],
    )


def _edge(
    alert: Alert,
    txn_id: str,
    gap_minutes: int,
    *,
    upstream: bool,
    amount: float = 90000.0,
    currency: str = "US Dollar",
) -> Edge:
    anchor = datetime.fromisoformat(alert["ts"])
    offset = timedelta(minutes=gap_minutes)
    ts = str(anchor - offset if upstream else anchor + offset)
    other = f"900|{txn_id.removeprefix('txn_').upper()}"
    return Edge(
        txn_id=txn_id,
        txn_date=ts[:10],
        ts=ts,
        sender=other if upstream else RECEIVER,
        receiver=SENDER if upstream else other,
        amount=amount,
        currency=currency,
        payment_format="ACH",
        gap_minutes=gap_minutes,
    )


def _totals(edges: list[Edge]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for edge in edges:
        totals[edge["currency"]] = totals.get(edge["currency"], 0.0) + edge["amount"]
    return totals


def _graph(
    alert: Alert,
    upstream: list[Edge],
    downstream: list[Edge],
    *,
    truncated: bool = False,
) -> CaseGraph:
    shown = upstream + downstream
    return CaseGraph(
        txn_id=alert["txn_id"],
        window_h=WINDOW_H,
        upstream=upstream,
        downstream=downstream,
        n_upstream=len(upstream),
        n_downstream=len(downstream),
        n_upstream_accounts=len({e["sender"] for e in upstream}),
        n_downstream_accounts=len({e["receiver"] for e in downstream}),
        per_currency_totals={
            "upstream": _totals(upstream),
            "downstream": _totals(downstream),
        },
        no_graph=not shown,
        edges_capped=False,
        window_truncated=truncated,
        horizon_end=config.AGENT_DATA_HORIZON,
        ids=list(
            dict.fromkeys(
                [alert["txn_id"], alert["sender"], alert["receiver"]]
                + [e["txn_id"] for e in shown]
                + [e["sender"] for e in shown]
                + [e["receiver"] for e in shown]
            )
        ),
    )


def _neighbour(txn_id: str, rank_day: int | None, txn_date: str = SAMPLE_DAY) -> NeighbourScore:
    scored = rank_day is not None
    return NeighbourScore(
        txn_id=txn_id,
        txn_date=txn_date,
        scored=scored,
        risk_score=0.5 if scored else None,
        risk_rank_global=rank_day * 10 if scored else None,
        risk_rank_day=rank_day,
        in_queue=scored,
        in_day_top_k=scored and rank_day <= config.AGENT_TOP_K,
        has_reasons=scored and rank_day <= 5000,
    )


def _neighbours(*items: NeighbourScore) -> NeighbourScores:
    return NeighbourScores(neighbours=list(items), ids=[n["txn_id"] for n in items])


def _decision(
    verdict: str,
    confidence: str,
    claims: list[Claim],
    *,
    case_id: str | None = None,
    promoted: tuple[str, ...] = (),
    note: str | None = None,
) -> Decision:
    return Decision(
        verdict=verdict,
        confidence=confidence,
        case_id=case_id,
        promoted_txn_ids=list(promoted),
        claims=claims,
        note=note,
    )


def _claim(code: str, *evidence_ids: str) -> Claim:
    return Claim(code=code, evidence_ids=list(evidence_ids))


def _result(
    alert: Alert,
    decision: Decision,
    *,
    graph: CaseGraph | None = None,
    neighbours: NeighbourScores | None = None,
    related: RelatedAlerts | None = None,
    repair: bool = False,
    fallback: bool = False,
) -> InvestigateResult:
    txn_id = alert["txn_id"]
    trace = [
        TraceStep(
            step=0,
            actor="code",
            tool="get_alert",
            inputs={"txn_id": txn_id, "txn_date": alert["txn_date"]},
            result=alert,
            latency_ms=2500,
        )
    ]

    def add(actor: str, tool: str, inputs: dict, result: dict) -> None:
        trace.append(
            TraceStep(
                step=len(trace),
                actor=actor,
                tool=tool,
                inputs=inputs,
                result=result,
                latency_ms=2500,
            )
        )

    if graph is not None:
        add("llm", "get_case_graph", {"txn_id": txn_id, "window_h": WINDOW_H}, graph)
    if neighbours is not None:
        add("llm", "get_neighbour_scores", {"txn_ids": list(neighbours["ids"])}, neighbours)
    if related is not None:
        add("llm", "get_related_alerts", {"accounts": [SENDER, RECEIVER]}, related)
    tool_steps = len(trace) - 1

    rejected = {"valid": False, "errors": ["Cited an ID that no tool returned."]}
    if repair or fallback:
        add("llm", "submit_decision", {"verdict": decision["verdict"]}, rejected)
    if fallback:
        add("llm", "submit_decision", {"verdict": decision["verdict"]}, rejected)
        add("code", "deterministic_baseline", {"txn_id": txn_id}, decision)
    else:
        add("llm", "submit_decision", decision, {"valid": True})

    return InvestigateResult(
        status="ok",
        txn_id=txn_id,
        txn_date=alert["txn_date"],
        window_h=WINDOW_H,
        alert=alert,
        decision=decision,
        rendered_claims=render_claims(
            decision, graph=graph, neighbours=neighbours, related=related
        ),
        graph=graph,
        neighbours=neighbours,
        related=related,
        stats=RunStats(
            model="sample",
            steps=tool_steps,
            tokens_in=5000 * (tool_steps + 1),
            tokens_out=300 * (tool_steps + 1),
            latency_ms=2500 * len(trace),
            validation="fallback" if fallback else ("repaired" if repair else "valid"),
            repair_used=repair or fallback,
            fallback=fallback,
        ),
        trace=trace,
    )


def _escalate() -> InvestigateResult:
    alert = _alert("txn_sampleescalate", 2)
    up = _edge(alert, "txn_sampleup1", 30, upstream=True)
    down = _edge(alert, "txn_sampledown1", 12, upstream=False)
    return _result(
        alert,
        _decision(
            "escalate",
            "high",
            [
                _claim("REASONS_CORROBORATED", alert["txn_id"], up["txn_id"]),
                _claim("FAST_CHAIN", down["txn_id"]),
            ],
        ),
        graph=_graph(alert, [up], [down]),
    )


def _merge() -> InvestigateResult:
    alert = _alert("txn_samplemerge", 5)
    up = _edge(alert, "txn_samplemergeup", 45, upstream=True)
    down = _edge(alert, "txn_samplemergedown", 20, upstream=False)
    related = RelatedAlerts(
        alerts=[
            RelatedAlert(
                txn_id="txn_samplemember1",
                txn_date=SAMPLE_DAY,
                ts="2022-09-14 11:00:00",
                risk_rank_day=40,
                account=SENDER,
                role="sender",
            )
        ],
        top_k=config.AGENT_TOP_K,
        window_h=WINDOW_H,
        ids=["txn_samplemember1", SENDER],
    )
    return _result(
        alert,
        _decision(
            "merge_into_case",
            "medium",
            [
                _claim("NEIGHBOURS_HIGH_RISK", up["txn_id"], down["txn_id"]),
                _claim("REPEAT_ACCOUNT_ALERTS", "txn_samplemember1"),
            ],
            case_id=case_id_for(alert["txn_id"]),
        ),
        graph=_graph(alert, [up], [down]),
        neighbours=_neighbours(_neighbour(up["txn_id"], 30), _neighbour(down["txn_id"], 80)),
        related=related,
    )


def _promote() -> InvestigateResult:
    alert = _alert("txn_samplepromote", 8)
    down = _edge(alert, "txn_samplelowrank", 15, upstream=False)
    return _result(
        alert,
        _decision(
            "promote",
            "medium",
            [_claim("FAST_CHAIN", down["txn_id"])],
            case_id=case_id_for(alert["txn_id"]),
            promoted=(down["txn_id"],),
        ),
        graph=_graph(alert, [], [down]),
        neighbours=_neighbours(_neighbour(down["txn_id"], 4200)),
    )


def _deprioritise() -> InvestigateResult:
    alert = _alert("txn_sampledeprioritise", 450)
    up = _edge(alert, "txn_sampleroutine", 2000, upstream=True)
    return _result(
        alert,
        _decision(
            "deprioritise",
            "medium",
            [_claim("REASONS_NOT_SUPPORTED", alert["txn_id"], up["txn_id"])],
        ),
        graph=_graph(alert, [up], []),
        neighbours=_neighbours(_neighbour(up["txn_id"], None, "2022-09-12")),
    )


def _need_more() -> InvestigateResult:
    alert = _alert("txn_sampleneedmore", 300)
    up = _edge(alert, "txn_samplepartial", 600, upstream=True)
    return _result(
        alert,
        _decision(
            "need_more",
            "low",
            [_claim("REASONS_NOT_SUPPORTED", alert["txn_id"])],
            note="The graph is thin, so this needs an analyst's review.",
        ),
        graph=_graph(alert, [up], []),
    )


def _no_reasons() -> InvestigateResult:
    alert = _alert("txn_samplenoreasons", 40000, reasons=False)
    down = _edge(alert, "txn_samplenoreasonsdown", 50, upstream=False)
    return _result(
        alert,
        _decision(
            "need_more",
            "low",
            [_claim("NO_REASONS_AVAILABLE", alert["txn_id"])],
        ),
        graph=_graph(alert, [], [down]),
    )


def _no_graph() -> InvestigateResult:
    alert = _alert("txn_samplenograph", 60)
    alert["amount"] = None
    alert["currency"] = None
    alert["payment_format"] = None
    return _result(
        alert,
        _decision(
            "deprioritise",
            "low",
            [_claim("NO_GRAPH_AVAILABLE", alert["txn_id"])],
        ),
        graph=_graph(alert, [], []),
    )


def _truncated() -> InvestigateResult:
    alert = _alert("txn_sampletruncated", 10, ts="2022-09-16 20:00:00")
    up = _edge(alert, "txn_sampletruncatedup", 25, upstream=True, currency="Euro")
    up2 = _edge(alert, "txn_sampletruncatedup2", 35, upstream=True)
    return _result(
        alert,
        _decision(
            "escalate",
            "low",
            [
                _claim("REASONS_CORROBORATED", alert["txn_id"], up["txn_id"]),
                _claim("TRUNCATED_WINDOW", alert["txn_id"]),
            ],
        ),
        graph=_graph(alert, [up, up2], [], truncated=True),
    )


def _repaired() -> InvestigateResult:
    alert = _alert("txn_samplerepaired", 15)
    down = _edge(alert, "txn_samplerepaireddown", 18, upstream=False)
    return _result(
        alert,
        _decision("escalate", "medium", [_claim("FAST_CHAIN", down["txn_id"])]),
        graph=_graph(alert, [], [down]),
        repair=True,
    )


def _fallback() -> InvestigateResult:
    alert = _alert("txn_samplefallback", 20)
    down = _edge(alert, "txn_samplefallbackdown", 22, upstream=False)
    return _result(
        alert,
        _decision("escalate", "low", [_claim("FAST_CHAIN", down["txn_id"])]),
        graph=_graph(alert, [], [down]),
        neighbours=_neighbours(_neighbour(down["txn_id"], 900)),
        fallback=True,
    )


def _not_found() -> InvestigateResult:
    txn_id = "txn_samplemissing"
    return InvestigateResult(
        status="not_found",
        txn_id=txn_id,
        txn_date=SAMPLE_DAY,
        window_h=WINDOW_H,
        alert=Alert(found=False, txn_id=txn_id, txn_date=SAMPLE_DAY, ids=[]),
        decision=None,
        rendered_claims=[],
        graph=None,
        neighbours=None,
        related=None,
        stats=None,
        trace=[],
    )


def sample_investigations() -> dict[str, InvestigateResult]:
    """One made-up result per situation the Streamlit screens must handle."""

    return {
        "escalate": _escalate(),
        "merge_into_case": _merge(),
        "promote": _promote(),
        "deprioritise": _deprioritise(),
        "need_more": _need_more(),
        "no_reasons": _no_reasons(),
        "no_graph": _no_graph(),
        "truncated": _truncated(),
        "repaired": _repaired(),
        "fallback": _fallback(),
        "not_found": _not_found(),
    }


def sample_decision_rows() -> list[DecisionRow]:
    """Made-up is459_agent_decisions rows, as the batch runner will store them."""

    rows: list[DecisionRow] = []
    for name, result in sample_investigations().items():
        if result["status"] != "ok":
            continue
        members = ("txn_samplemember1",) if name == "merge_into_case" else ()
        rows.extend(
            build_decision_rows(
                result,
                run_id=SAMPLE_RUN_ID,
                created_at=SAMPLE_CREATED_AT,
                member_txn_ids=members,
            )
        )
    return rows
