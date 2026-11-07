"""Made-up agent results for building the app without running the agent.
All IDs, numbers and sentences are invented, so never present them as real."""

from datetime import datetime, timedelta

import pandas as pd

from src import config
from src.agent import baseline, tools
from src.agent.brief import case_id_for
from src.agent.cases import Case
from src.agent.contracts import (
    AccountActivity,
    Alert,
    CaseGraph,
    CaseMember,
    DecisionRow,
    InvestigateResult,
    NeighbourScore,
    NeighbourScores,
    RelatedAlert,
    RelatedAlerts,
    RunStats,
    TraceStep,
)
from src.agent.decisions import build_decision_rows
from src.agent.evidence import RunEvidence

SAMPLE_DAY = "2022-09-14"
SAMPLE_RUN_ID = "sample-run"
SAMPLE_CREATED_AT = "2026-11-06T00:00:00+00:00"
SENDER = "100|SAMPLE1"
RECEIVER = "200|SAMPLE2"
WINDOW_H = 48

_GRAPH_COLUMNS = [
    "edge_role",
    "transaction_id",
    "source_account_key",
    "target_account_key",
    "transaction_timestamp",
    "gap_minutes",
    "amount_paid",
    "payment_currency",
    "payment_format",
]
_ACTIVITY_COLUMNS = [
    "direction",
    "transaction_id",
    "counterparty_key",
    "transaction_timestamp",
    "offset_minutes",
    "amount_paid",
    "payment_currency",
    "payment_format",
]
_HISTORY_COLUMNS = ["txn_date", "direction", "payment_currency", "n_payments", "total_amount"]


def _alert(
    txn_id: str,
    rank_day: int,
    *,
    ts: str = "2022-09-14 10:00:00",
    sender: str = SENDER,
    receiver: str = RECEIVER,
    self_transfer: bool = False,
) -> Alert:
    return Alert(
        found=True,
        txn_id=txn_id,
        txn_date=ts[:10],
        ts=ts,
        sender=sender,
        receiver=receiver,
        amount=None if self_transfer else 250000.0,
        currency=None if self_transfer else "US Dollar",
        payment_format=None if self_transfer else "ACH",
        risk_score=0.75,
        risk_rank_global=rank_day * 10,
        risk_rank_day=rank_day,
        in_queue=True,
        in_day_top_k=rank_day <= config.AGENT_TOP_K,
        ids=[txn_id, sender, receiver],
    )


def _edge_row(
    alert: Alert,
    role: str,
    txn_id: str,
    gap_minutes: int,
    *,
    account: str | None = None,
    amount: float = 90000.0,
    currency: str = "US Dollar",
) -> dict:
    upstream = role == "UPSTREAM"
    anchor = datetime.fromisoformat(alert["ts"])
    ts = anchor - timedelta(minutes=gap_minutes) if upstream else anchor + timedelta(minutes=gap_minutes)
    other = account or f"900|{txn_id.removeprefix('txn_').upper()}"
    return {
        "edge_role": role,
        "transaction_id": txn_id,
        "source_account_key": other if upstream else alert["receiver"],
        "target_account_key": alert["sender"] if upstream else other,
        "transaction_timestamp": f"{ts:%Y-%m-%d %H:%M:%S}.000",
        "gap_minutes": str(gap_minutes),
        "amount_paid": f"{amount:.6f}",
        "payment_currency": currency,
        "payment_format": "ACH",
    }


def _up(alert: Alert, txn_id: str, gap: int, **kwargs) -> dict:
    return _edge_row(alert, "UPSTREAM", txn_id, gap, **kwargs)


def _down(alert: Alert, txn_id: str, gap: int, **kwargs) -> dict:
    return _edge_row(alert, "DOWNSTREAM", txn_id, gap, **kwargs)


def _graph(alert: Alert, *rows: dict) -> CaseGraph:
    return tools.case_graph_from_frame(
        alert, pd.DataFrame(list(rows), columns=_GRAPH_COLUMNS), WINDOW_H
    )


def _payment(
    alert: Alert,
    direction: str,
    txn_id: str,
    counterparty: str,
    offset_minutes: int,
    amount: float = 4000.0,
) -> dict:
    ts = datetime.fromisoformat(alert["ts"]) + timedelta(minutes=offset_minutes)
    return {
        "direction": direction,
        "transaction_id": txn_id,
        "counterparty_key": counterparty,
        "transaction_timestamp": f"{ts:%Y-%m-%d %H:%M:%S}.000",
        "offset_minutes": str(offset_minutes),
        "amount_paid": f"{amount:.6f}",
        "payment_currency": "US Dollar",
        "payment_format": "ACH",
    }


def _history(*daily: tuple[str, str, int, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "txn_date": day,
                "direction": direction,
                "payment_currency": "US Dollar",
                "n_payments": str(n),
                "total_amount": f"{total:.2f}",
            }
            for day, direction, n, total in daily
        ],
        columns=_HISTORY_COLUMNS,
    )


def _activity(
    alert: Alert, side: str, *payments: dict, history: pd.DataFrame | None = None
) -> AccountActivity:
    return tools.account_activity_from_frames(
        alert,
        side,
        pd.DataFrame(list(payments), columns=_ACTIVITY_COLUMNS),
        history if history is not None else _history(),
        WINDOW_H,
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
    )


def _neighbours(*items: NeighbourScore) -> NeighbourScores:
    return NeighbourScores(neighbours=list(items), ids=[n["txn_id"] for n in items])


def _related(alert: Alert, *others: tuple[str, int, str]) -> RelatedAlerts:
    """``others`` are (txn id, day rank, account it touches). All are receivers of that payment."""

    alerts = [
        RelatedAlert(
            txn_id=txn_id,
            txn_date=SAMPLE_DAY,
            ts=alert["ts"],
            risk_rank_day=rank,
            account=account,
            role="receiver",
        )
        for txn_id, rank, account in others
    ]
    return RelatedAlerts(
        alerts=alerts,
        top_k=config.AGENT_TOP_K,
        window_h=WINDOW_H,
        ids=[a["txn_id"] for a in alerts] + [a["account"] for a in alerts],
    )


def _result(
    members: list[tuple[Alert, CaseGraph, AccountActivity, AccountActivity]],
    neighbours: NeighbourScores,
    related: RelatedAlerts,
    *,
    summary: str | None = None,
    repair: bool = False,
    fallback: bool = False,
) -> InvestigateResult:
    """Runs the real code-only brief assembly over invented tool results, then dresses it as an LLM run if asked.

    ``summary=None`` gives the code-only brief. With a summary the result looks like an LLM brief.
    """

    ids = tuple(alert["txn_id"] for alert, *_ in members)
    case = Case(case_id_for(ids[0]), SAMPLE_DAY, ids[0], ids)
    evidence = RunEvidence(
        representative=ids[0],
        members=ids,
        txn_dates={txn_id: SAMPLE_DAY for txn_id in ids},
    )
    trace: list[TraceStep] = []

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

    case_members: list[CaseMember] = []
    for alert, graph, sender, receiver in members:
        evidence.record("get_alert", alert)
        evidence.record("get_case_graph", graph)
        evidence.record("get_account_activity", sender)
        evidence.record("get_account_activity", receiver)
        case_members.append(CaseMember(alert=alert, graph=graph))
    evidence.record("get_neighbour_scores", neighbours)
    evidence.record("get_related_alerts", related)

    lead, lead_graph, lead_sender, lead_receiver = members[0]
    actor = "code" if summary is None else "llm"
    add("code", "get_alert", {"txn_id": lead["txn_id"], "txn_date": SAMPLE_DAY}, lead)
    add(actor, "get_case_graph", {"txn_id": lead["txn_id"], "window_h": WINDOW_H}, lead_graph)
    for side, activity in (("sender", lead_sender), ("receiver", lead_receiver)):
        add(
            actor,
            "get_account_activity",
            {"txn_id": lead["txn_id"], "side": side, "window_h": WINDOW_H},
            activity,
        )
    add(actor, "get_neighbour_scores", {"txn_ids": list(neighbours["ids"])}, neighbours)
    add(actor, "get_related_alerts", {"txn_id": lead["txn_id"]}, related)

    result = baseline.brief_from_evidence(case, WINDOW_H, evidence, case_members, trace)

    stats = RunStats(
        model="sample",
        steps=0,
        tokens_in=0,
        tokens_out=0,
        latency_ms=2500 * len(trace),
        validation="valid",
        repair_used=False,
        fallback=False,
    )
    if summary is not None:
        rejected = {"valid": False, "errors": ["Cited an ID that no tool returned."]}
        submitted = {"summary": summary}
        stats.update(steps=3, tokens_in=14000, tokens_out=700)
        if repair or fallback:
            add("llm", "submit_brief", submitted, rejected)
            stats.update(validation="repaired", repair_used=True, tokens_in=19000)
        if fallback:
            add("llm", "submit_brief", submitted, rejected)
            add("code", "fallback", {}, {"reason": "Brief still invalid after one repair."})
            stats.update(validation="fallback", fallback=True)
            result["fallback"] = True
        else:
            result["summary"] = summary
            add("llm", "submit_brief", submitted, {"valid": True, "errors": []})
    result["stats"] = stats
    result["trace"] = trace
    return result


def _pass_through(txn_id: str = "txn_samplepass", **flavour) -> InvestigateResult:
    alert = _alert(txn_id, 2)
    graph = _graph(
        alert,
        _up(alert, "txn_samplepassup", 120, amount=200000.0),
        _down(alert, "txn_samplepassdown1", 25, amount=95000.0),
        _down(alert, "txn_samplepassdown2", 90, amount=85000.0),
    )
    history = _history(("2022-09-12", "in", 1, 20000.0), ("2022-09-13", "out", 1, 18000.0))
    return _result(
        [
            (
                alert,
                graph,
                _activity(alert, "sender", history=history),
                _activity(alert, "receiver", history=history),
            )
        ],
        _neighbours(_neighbour("txn_samplepassdown1", None)),
        _related(alert),
        **flavour,
    )


def _fan_out_case() -> InvestigateResult:
    alert = _alert("txn_samplefan", 3)
    member = _alert("txn_samplefanmember", 8, receiver="300|SAMPLE3")
    outgoing = [
        _payment(alert, "out", f"txn_samplefanout{n}", f"950|FANOUT{n}", 10 * n)
        for n in range(1, 7)
    ]
    history = _history(("2022-09-12", "out", 1, 3000.0))
    members = [
        (
            alert,
            _graph(alert, _up(alert, "txn_samplefanup", 300, amount=40000.0)),
            _activity(alert, "sender", *outgoing, history=history),
            _activity(
                alert,
                "receiver",
                *[
                    _payment(alert, "in", f"txn_samplefanin{n}", f"960|FANIN{n}", -10 * n)
                    for n in range(1, 7)
                ],
                history=history,
            ),
        ),
        (
            member,
            _graph(member, _down(member, "txn_samplefanmemberdown", 700, amount=1000.0)),
            _activity(member, "sender", history=history),
            _activity(member, "receiver", history=history),
        ),
    ]
    return _result(
        members,
        _neighbours(_neighbour("txn_samplefanup", 40), _neighbour("txn_samplefanout1", None)),
        _related(alert, ("txn_samplefanmember", 8, SENDER)),
        summary="One account paid out to many others, and a second top alert shares an account.",
    )


def _money_returns() -> InvestigateResult:
    alert = _alert("txn_samplereturn", 5)
    source = "400|SAMPLESOURCE"
    graph = _graph(
        alert,
        _up(alert, "txn_samplereturnup", 100, account=source, amount=100000.0),
        _down(alert, "txn_samplereturnback", 200, account=source, amount=95000.0),
    )
    history = _history(("2022-09-13", "in", 1, 5000.0))
    return _result(
        [
            (
                alert,
                graph,
                _activity(alert, "sender", history=history),
                _activity(alert, "receiver", history=history),
            )
        ],
        _neighbours(),
        _related(alert),
        summary="Money went out and came back to an account that had paid in.",
    )


def _quiet_truncated() -> InvestigateResult:
    alert = _alert("txn_samplequiet", 30, ts="2022-09-16 20:00:00")
    graph = _graph(
        alert,
        _up(alert, "txn_samplequietup", 900, amount=50000.0),
        _down(alert, "txn_samplequietdown", 100, amount=2000.0),
    )
    history = _history(("2022-09-14", "in", 1, 1000.0), ("2022-09-15", "out", 1, 900.0))
    return _result(
        [
            (
                alert,
                graph,
                _activity(alert, "sender", history=history),
                _activity(alert, "receiver", history=history),
            )
        ],
        _neighbours(_neighbour("txn_samplequietdown", None)),
        _related(alert),
        summary="No pattern was found, and only a few hours after the alert are visible.",
    )


def _no_graph() -> InvestigateResult:
    alert = _alert("txn_samplenograph", 12, receiver=SENDER, self_transfer=True)
    return _result(
        [(alert, _graph(alert), _activity(alert, "sender"), _activity(alert, "receiver"))],
        _neighbours(),
        _related(alert),
        summary="No payments were found around this alert, so there is nothing to compare.",
    )


def _not_found() -> InvestigateResult:
    txn_id = "txn_samplemissing"
    return InvestigateResult(
        status="not_found",
        txn_id=txn_id,
        txn_date=SAMPLE_DAY,
        window_h=WINDOW_H,
        alert=Alert(found=False, txn_id=txn_id, txn_date=SAMPLE_DAY, ids=[]),
        case_id=None,
        summary=None,
        findings=[],
        gaps=[],
        next_checks=[],
        money_flow=[],
        members=[],
        fallback=False,
        stats=None,
        trace=[],
    )


def sample_investigations() -> dict[str, InvestigateResult]:
    """One made-up result per situation the Streamlit screens must handle."""

    return {
        "pass_through": _pass_through(
            summary="Money came in and went straight on to other accounts."
        ),
        "fan_out_case": _fan_out_case(),
        "money_returns": _money_returns(),
        "quiet_truncated": _quiet_truncated(),
        "no_graph": _no_graph(),
        "code_only": _pass_through("txn_samplecodeonly"),
        "repaired": _pass_through(
            "txn_samplerepaired",
            summary="Money came in and went straight on to other accounts.",
            repair=True,
        ),
        "fallback": _pass_through(
            "txn_samplefallback",
            summary="Money came in and went straight on to other accounts.",
            fallback=True,
        ),
        "not_found": _not_found(),
    }


def sample_decision_rows() -> list[DecisionRow]:
    """Made-up is459_agent_decisions rows, as the batch runner will store them."""

    rows: list[DecisionRow] = []
    for name, result in sample_investigations().items():
        if result["status"] != "ok":
            continue
        members = ("txn_samplefanmember",) if name == "fan_out_case" else ()
        rows.extend(
            build_decision_rows(
                result,
                run_id=SAMPLE_RUN_ID,
                created_at=SAMPLE_CREATED_AT,
                member_txn_ids=members,
            )
        )
    return rows
