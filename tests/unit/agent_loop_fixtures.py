"""Fakes for the agent loop, validator and detector tests. No AWS, no real model.

Tool results are built by the real pure functions (``tools.case_graph_from_frame`` and
``tools.account_activity_from_frames``) from invented rows, so the rules see real shapes.
"""

import copy

import pandas as pd

from src import config
from src.agent import tools
from src.agent.cases import Case
from src.agent.evidence import RunEvidence
from src.agent.llm import AgentTurn, LLMUsage, ToolCall

DAY = "2022-09-14"
TS = "2022-09-14 10:00:00"

GRAPH_COLUMNS = [
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
ACTIVITY_COLUMNS = [
    "direction",
    "transaction_id",
    "counterparty_key",
    "transaction_timestamp",
    "offset_minutes",
    "amount_paid",
    "payment_currency",
    "payment_format",
]


def alert_result(txn_id, *, found=True):
    if not found:
        return {"found": False, "txn_id": txn_id, "txn_date": DAY, "ids": []}
    suffix = txn_id[-1].upper()
    return {
        "found": True,
        "txn_id": txn_id,
        "txn_date": DAY,
        "ts": TS,
        "sender": f"1|{suffix}",
        "receiver": "2|B",
        "amount": 1000.5,
        "currency": "US Dollar",
        "payment_format": "Wire",
        "risk_score": 0.9123,
        "risk_rank_global": 7,
        "risk_rank_day": 2,
        "in_queue": True,
        "in_day_top_k": True,
        "ids": [txn_id, f"1|{suffix}", "2|B"],
    }


def _graph_row(alert, role, txn_id, gap, amount, currency="US Dollar", account=None):
    upstream = role == "UPSTREAM"
    other = account or ("9|SRC" if upstream else "3|C")
    ts = "2022-09-14 09:00:00.000" if upstream else "2022-09-14 11:00:00.000"
    return {
        "edge_role": role,
        "transaction_id": txn_id,
        "source_account_key": other if upstream else alert["receiver"],
        "target_account_key": alert["sender"] if upstream else other,
        "transaction_timestamp": ts,
        "gap_minutes": str(gap),
        "amount_paid": f"{amount:.6f}",
        "payment_currency": currency,
        "payment_format": "ACH",
    }


def graph_result(
    txn_id,
    downstream=("txn_low", "txn_high"),
    upstream=("txn_up",),
    *,
    as_of=None,
    window_h=48,
    down_gap=15,
    down_amount=400.0,
    up_amount=1000.0,
    returned_to=None,
):
    """A graph: ``upstream`` payments of 1000 in, ``downstream`` payments of 400 out (defaults)."""

    alert = alert_result(txn_id)
    rows = [_graph_row(alert, "UPSTREAM", t, 60, up_amount) for t in upstream]
    rows += [
        _graph_row(
            alert, "DOWNSTREAM", t, down_gap, down_amount, account=returned_to
        )
        for t in downstream
    ]
    return tools.case_graph_from_frame(
        alert,
        pd.DataFrame(rows, columns=GRAPH_COLUMNS),
        window_h,
        as_of=as_of,
    )


def activity_result(txn_id, side, *, fan=0, incoming=0, as_of=None, window_h=48):
    """Account activity: ``fan`` payments out to that many different accounts, ``incoming`` in."""

    alert = alert_result(txn_id)
    rows = [
        {
            "direction": "out",
            "transaction_id": f"txn_{side[0]}o{n}",
            "counterparty_key": f"8|O{n}",
            "transaction_timestamp": "2022-09-14 10:30:00.000",
            "offset_minutes": str(10 * n),
            "amount_paid": "100.000000",
            "payment_currency": "US Dollar",
            "payment_format": "ACH",
        }
        for n in range(1, fan + 1)
    ] + [
        {
            "direction": "in",
            "transaction_id": f"txn_{side[0]}i{n}",
            "counterparty_key": f"7|I{n}",
            "transaction_timestamp": "2022-09-14 09:30:00.000",
            "offset_minutes": str(-10 * n),
            "amount_paid": "100.000000",
            "payment_currency": "US Dollar",
            "payment_format": "ACH",
        }
        for n in range(1, incoming + 1)
    ]
    return tools.account_activity_from_frames(
        alert,
        side,
        pd.DataFrame(rows, columns=ACTIVITY_COLUMNS),
        pd.DataFrame(
            columns=["txn_date", "direction", "payment_currency", "n_payments", "total_amount"]
        ),
        window_h,
        as_of=as_of,
    )


def neighbour_result(txn_id, rank):
    scored = rank is not None
    return {
        "txn_id": txn_id,
        "txn_date": DAY,
        "scored": scored,
        "risk_score": 0.5 if scored else None,
        "risk_rank_global": rank,
        "risk_rank_day": rank,
        "in_queue": scored,
        "in_day_top_k": scored and rank <= config.AGENT_TOP_K,
    }


def neighbour_scores_result(ranks):
    items = [neighbour_result(t, r) for t, r in ranks.items()]
    return {"neighbours": items, "ids": [n["txn_id"] for n in items]}


def related_result(*txn_ids):
    alerts = [
        {
            "txn_id": t,
            "txn_date": DAY,
            "ts": TS,
            "risk_rank_day": 3,
            "account": "2|B",
            "role": "receiver",
        }
        for t in txn_ids
    ]
    return {
        "alerts": alerts,
        "top_k": config.AGENT_TOP_K,
        "window_h": 48,
        "ids": list(txn_ids) + ["2|B"] if txn_ids else [],
    }


def case_of(*members):
    return Case(f"case_{members[0]}", DAY, members[0], tuple(members))


def make_evidence(
    members=("txn_a",),
    *,
    downstream=("txn_low", "txn_high"),
    upstream=("txn_up",),
    ranks=None,
    truncated=False,
    related=None,
    fan=0,
    incoming=0,
    activities=True,
):
    """Evidence after the LLM fetched the seed, graph, activity and neighbour scores."""

    ranks = {"txn_low": 900, "txn_high": 12} if ranks is None else ranks
    as_of = "2022-09-14 12:00:00" if truncated else None
    evidence = RunEvidence(
        representative=members[0],
        members=tuple(members),
        txn_dates={m: DAY for m in members},
    )
    evidence.record("get_alert", alert_result(members[0]))
    evidence.record(
        "get_case_graph",
        graph_result(members[0], downstream, upstream, as_of=as_of),
    )
    if activities:
        evidence.record(
            "get_account_activity",
            activity_result(members[0], "sender", fan=fan, as_of=as_of),
        )
        evidence.record(
            "get_account_activity",
            activity_result(members[0], "receiver", incoming=incoming, as_of=as_of),
        )
    if ranks:
        evidence.record("get_neighbour_scores", neighbour_scores_result(ranks))
    if related is not None:
        evidence.record("get_related_alerts", related)
    return evidence


def valid_payload(**overrides):
    payload = {
        "findings": [
            {"code": "PASS_THROUGH", "evidence_ids": ["txn_high", "txn_up"]},
            {"code": "NEIGHBOURS_HIGH_RISK", "evidence_ids": ["txn_high"]},
        ],
        "next_checks": ["CHECK_ONWARD_ACCOUNTS"],
        "summary": "Money came in and went on quickly.",
    }
    payload.update(overrides)
    return payload


# --- Loop fakes


def turn(*calls, text="", tokens_in=100, tokens_out=20):
    """A scripted model reply. Each call is (name, input)."""

    tool_calls = tuple(
        ToolCall(id=f"use_{i}_{name}", name=name, input=args)
        for i, (name, args) in enumerate(calls)
    )
    content = ([{"text": text}] if text else []) + [
        {"toolUse": {"toolUseId": c.id, "name": c.name, "input": c.input}}
        for c in tool_calls
    ]
    return AgentTurn(
        content=content,
        text=text,
        tool_calls=tool_calls,
        usage=LLMUsage(tokens_in, tokens_out),
        latency_ms=5,
        stop_reason="tool_use" if tool_calls else "end_turn",
    )


class ScriptedLLM:
    """Replies from a fixed script, recording what it was sent."""

    model_id = "scripted-model"

    def __init__(self, *turns, error_at=None):
        self._turns = list(turns)
        self._error_at = error_at
        self.calls: list[dict] = []

    def converse(self, *, system, messages, tools, force_tool, max_tokens, temperature):
        self.calls.append(
            {
                "system": system,
                "messages": copy.deepcopy(messages),
                "tools": tools,
                "force_tool": force_tool,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self._error_at is not None and len(self.calls) - 1 == self._error_at:
            from src.agent.llm import LLMClientError

            raise LLMClientError("throttled")
        assert self._turns, (
            "The loop called the model more times than the script allows."
        )
        return self._turns.pop(0)


class CallLog(list):
    """The tool calls made, plus the cutoff each data-reading call was given."""

    def __init__(self):
        super().__init__()
        self.as_of_seen: list = []


def stub_tools(
    monkeypatch,
    *,
    ranks=None,
    downstream=None,
    truncated=False,
    fan=0,
    found=True,
):
    """Replace the data-reading tools with fixed results for the case txn_a, txn_b."""

    ranks = {"txn_low": 900, "txn_high": 12} if ranks is None else ranks
    downstream = list(ranks) if downstream is None else downstream
    calls = CallLog()
    as_of_seen = calls.as_of_seen

    def get_alert(data, txn_id, txn_date, top_k):
        calls.append(("get_alert", txn_id))
        return alert_result(txn_id, found=found)

    def get_case_graph(data, alert, window_h, as_of=None):
        calls.append(("get_case_graph", alert["txn_id"], window_h))
        as_of_seen.append(as_of)
        return graph_result(
            alert["txn_id"],
            downstream,
            window_h=window_h,
            as_of="2022-09-14 12:00:00" if truncated else as_of,
        )

    def get_account_activity(data, alert, side, window_h, as_of=None):
        calls.append(("get_account_activity", alert["txn_id"], side, window_h))
        as_of_seen.append(as_of)
        return activity_result(
            alert["txn_id"],
            side,
            fan=fan if side == "sender" else 0,
            window_h=window_h,
            as_of="2022-09-14 12:00:00" if truncated else as_of,
        )

    def get_neighbour_scores(data, txn_ids, txn_dates, top_k):
        calls.append(("get_neighbour_scores", tuple(txn_ids)))
        return neighbour_scores_result({t: ranks.get(t) for t in txn_ids})

    def get_related_alerts(
        data, accounts, ts, window_h, exclude_txn_ids, top_k, as_of=None
    ):
        as_of_seen.append(as_of)
        calls.append(("get_related_alerts", tuple(accounts), tuple(exclude_txn_ids)))
        return related_result("txn_b")

    monkeypatch.setattr(tools, "get_alert", get_alert)
    monkeypatch.setattr(tools, "get_case_graph", get_case_graph)
    monkeypatch.setattr(tools, "get_account_activity", get_account_activity)
    monkeypatch.setattr(tools, "get_neighbour_scores", get_neighbour_scores)
    monkeypatch.setattr(tools, "get_related_alerts", get_related_alerts)
    return calls
