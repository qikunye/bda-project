"""Fakes for the agent loop and validator tests. No AWS, no real model."""

import copy

from src import config
from src.agent import tools
from src.agent.cases import Case
from src.agent.llm import AgentTurn, LLMUsage, ToolCall
from src.agent.validate import RunEvidence

DAY = "2022-09-14"
TS = "2022-09-14 10:00:00"


def alert_result(txn_id, *, reasons=True, found=True):
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
        "reasons": [
            {
                "feature": "f",
                "sentence": "high amount",
                "direction": "up",
                "contribution": 0.4,
            }
        ]
        if reasons
        else None,
        "ids": [txn_id, f"1|{suffix}", "2|B"],
    }


def edge_result(txn_id, gap=15):
    return {
        "txn_id": txn_id,
        "txn_date": DAY,
        "ts": "2022-09-14 11:00:00",
        "sender": "2|B",
        "receiver": "3|C",
        "amount": 250.0,
        "currency": "US Dollar",
        "payment_format": "ACH",
        "gap_minutes": gap,
    }


def graph_result(txn_id, downstream=(), *, truncated=False):
    edges = [edge_result(t) for t in downstream]
    return {
        "txn_id": txn_id,
        "window_h": 48,
        "upstream": [],
        "downstream": edges,
        "n_upstream": 0,
        "n_downstream": len(edges),
        "n_upstream_accounts": 0,
        "n_downstream_accounts": len(edges),
        "per_currency_totals": {"upstream": {}, "downstream": {}},
        "no_graph": not edges,
        "edges_capped": False,
        "window_truncated": truncated,
        "horizon_end": config.AGENT_DATA_HORIZON,
        "ids": [txn_id, "2|B", "3|C"] + [e["txn_id"] for e in edges],
    }


def neighbour_result(txn_id, rank, *, scored=True):
    return {
        "txn_id": txn_id,
        "txn_date": DAY,
        "scored": scored,
        "risk_score": 0.5 if scored else None,
        "risk_rank_global": rank,
        "risk_rank_day": rank,
        "in_queue": rank is not None,
        "in_day_top_k": rank is not None and rank <= config.AGENT_TOP_K,
        "has_reasons": False,
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
    reasons=True,
    downstream=("txn_low", "txn_high"),
    ranks=None,
    truncated=False,
    related=None,
):
    """Evidence after the LLM fetched the seed, the graph and the neighbour scores."""

    ranks = {"txn_low": 900, "txn_high": 12} if ranks is None else ranks
    evidence = RunEvidence(
        representative=members[0],
        members=tuple(members),
        txn_dates={m: DAY for m in members},
    )
    evidence.record("get_alert", alert_result(members[0], reasons=reasons), "{}")
    evidence.record(
        "get_case_graph",
        graph_result(members[0], downstream, truncated=truncated),
        "{}",
    )
    if ranks:
        evidence.record("get_neighbour_scores", neighbour_scores_result(ranks), "{}")
    if related is not None:
        evidence.record("get_related_alerts", related, "{}")
    return evidence


def valid_payload(**overrides):
    payload = {
        "verdict": "raise",
        "confidence": "medium",
        "claims": [
            {"code": "FAST_CHAIN", "evidence_ids": ["txn_low"]},
            {"code": "NEIGHBOURS_HIGH_RISK", "evidence_ids": ["txn_high"]},
        ],
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
    reasons=True,
    found=True,
):
    """Replace the data-reading tools with fixed results for the case txn_a, txn_b."""

    ranks = {"txn_low": 900, "txn_high": 12} if ranks is None else ranks
    downstream = list(ranks) if downstream is None else downstream
    calls = CallLog()
    as_of_seen = calls.as_of_seen

    def get_alert(data, txn_id, txn_date, top_k):
        calls.append(("get_alert", txn_id))
        return alert_result(txn_id, reasons=reasons, found=found)

    def get_case_graph(data, alert, window_h, as_of=None):
        calls.append(("get_case_graph", alert["txn_id"], window_h))
        as_of_seen.append(as_of)
        graph = graph_result(alert["txn_id"], downstream, truncated=truncated)
        graph["window_h"] = window_h
        return graph

    def get_neighbour_scores(data, txn_ids, txn_dates, top_k):
        calls.append(("get_neighbour_scores", tuple(txn_ids)))
        return neighbour_scores_result({t: ranks[t] for t in txn_ids})

    def get_related_alerts(
        data, accounts, ts, window_h, exclude_txn_ids, top_k, as_of=None
    ):
        as_of_seen.append(as_of)
        calls.append(("get_related_alerts", tuple(accounts), tuple(exclude_txn_ids)))
        return related_result("txn_b")

    monkeypatch.setattr(tools, "get_alert", get_alert)
    monkeypatch.setattr(tools, "get_case_graph", get_case_graph)
    monkeypatch.setattr(tools, "get_neighbour_scores", get_neighbour_scores)
    monkeypatch.setattr(tools, "get_related_alerts", get_related_alerts)
    return calls
