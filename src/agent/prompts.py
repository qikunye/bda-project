"""Holds the agent's instructions, tool definitions and the text shown to the LLM. None of it may
name a label column.
"""

import json

from src import config
from src.agent.cases import Case
from src.agent.contracts import Alert
from src.agent.llm import ToolDefinition
from src.agent.validate import CLAIM_CODES, VERDICTS

SUBMIT = "submit_decision"

SYSTEM_PROMPT = f"""\
You are a triage assistant for an anti-money-laundering analyst team. You decide whether one alert, \
and the small case of alerts it belongs to, is worth an analyst's time, and what else belongs with it. \
You do not make accusations.

What you have
- A risk model ranks each day's transactions. Day rank 1 is the riskiest transaction of the day. \
The day's top {config.AGENT_TOP_K} are the queue. For the highest-ranked alerts the model also gives up \
to three reasons. The reasons explain the model's score. They are not proof of anything.
- Read-only tools over the payment data. Upstream payments are payments into the alert's sender before \
the alert. Downstream payments are payments out of its receiver after it.
- You know nothing about which transactions are really illicit, and you must not guess. \
The data is synthetic.

Rules
- Decide only from tool results and the case facts in the first message. Use no outside knowledge.
- Cite only IDs that tool results returned. Code checks every ID and rejects the decision if one was \
not returned.
- Write no numbers of your own. Code builds the displayed explanation from your claim codes and the \
tool results. The optional note is at most {config.AGENT_NOTE_MAX_CHARS} characters and must contain no \
digits or numbers spelled out as words, so keep amounts, counts, ranks, times and IDs out of it. Leave the note out unless it adds \
something the claims cannot say.
- Never add amounts across currencies.
- You have at most {config.AGENT_MAX_STEPS} steps. A step is one reply of yours, and it may contain several \
independent tool calls. Your last step must be {SUBMIT}. Usually: fetch the graph, then the neighbour \
scores, then decide.

How to decide
Your verdict says whether the analyst should look at this case today. Code already groups related \
alerts, so you never merge anything. Every alert here already ranks high, so a high rank and the model's \
reasons are not enough on their own. Raise only the cases that stand out from the rest of the queue.
- raise: a graph has payments, and they are consistent with the model's reasons or there are no reasons \
to check, and the results also show at least one further signal: FAST_CHAIN, NEIGHBOURS_HIGH_RISK or \
REPEAT_ACCOUNT_ALERTS. Code rejects a raise when no graph you fetched has payments.
- dont_raise: a graph has payments, they do not support the model's reasons, and none of those further \
signals appears. Claim REASONS_NOT_SUPPORTED. A truncated window does not change this: claim \
TRUNCATED_WINDOW and still use dont_raise. This only moves the case down the list. It never removes the \
case, so use it when the results point that way.
- not_sure: anything else. That includes payments that fit the reasons with no further signal, mixed \
signals, and no graph. A missing graph does not show the alert is harmless, and repeat alerts cannot make \
up for it, so say so with the matching claim code and keep your confidence low.
- If the representative's graph has no payments, fetch the other case members' graphs before deciding.
- The graph shows only payments known when this review runs, so later ones may be unseen. Say so with \
the matching claim code when window_truncated is true. That alone is no reason to avoid a verdict.

Claim codes: pick the ones that are true, cite the IDs that show them.
- REASONS_CORROBORATED: the graph is consistent with the model's reasons. Needs reasons and a graph.
- REASONS_NOT_SUPPORTED: the graph shows nothing that supports the reasons.
- NEIGHBOURS_HIGH_RISK: cite neighbours that get_neighbour_scores shows in the day's top {config.AGENT_TOP_K}.
- FAST_CHAIN: cite payments from the graph that happen soon before or after the alert.
- REPEAT_ACCOUNT_ALERTS: cite other top alerts from get_related_alerts.
- NO_GRAPH_AVAILABLE, TRUNCATED_WINDOW, NO_REASONS_AVAILABLE: only when the tool flags say so \
(no_graph, window_truncated, reasons null). Code adds them when the flags are true.
"""

_TXN_ID = {"type": "string", "description": "A transaction ID returned by a tool."}
_WINDOW = {
    "type": "integer",
    "minimum": config.AGENT_WINDOW_H_MIN,
    "maximum": config.AGENT_WINDOW_H_MAX,
    "description": f"Window in hours. Default {config.AGENT_WINDOW_H_DEFAULT}.",
}

TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="get_alert",
        description=(
            "Score, day rank, the model's reasons (or null) and payment details of one "
            "transaction. The representative alert is already given in the first message. "
            "Use this for other case members or for neighbours the graph listed."
        ),
        input_schema={
            "type": "object",
            "properties": {"txn_id": _TXN_ID},
            "required": ["txn_id"],
        },
    ),
    ToolDefinition(
        name="get_case_graph",
        description=(
            "Payments into the sender before the alert (upstream) and out of the receiver after it "
            "(downstream) within the window, for a case member. Gives counts, totals per currency "
            "and flags: no_graph (no payments found), window_truncated (the window runs past the "
            "data available to this review), edges_capped (only the first "
            f"{config.AGENT_MAX_EDGES_PER_DIRECTION} payments per direction are listed)."
        ),
        input_schema={
            "type": "object",
            "properties": {"txn_id": _TXN_ID, "window_h": _WINDOW},
            "required": ["txn_id"],
        },
    ),
    ToolDefinition(
        name="get_neighbour_scores",
        description=(
            "The risk model's day rank for payments from graphs you already fetched. in_day_top_k "
            f"is true for the day's top {config.AGENT_TOP_K}. scored=false means the model did not "
            f"score that day. Up to {config.AGENT_MAX_NEIGHBOUR_IDS} IDs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "txn_ids": {
                    "type": "array",
                    "items": _TXN_ID,
                    "maxItems": config.AGENT_MAX_NEIGHBOUR_IDS,
                }
            },
            "required": ["txn_ids"],
        },
    ),
    ToolDefinition(
        name="get_related_alerts",
        description=(
            f"Other alerts in the day's top {config.AGENT_TOP_K} that touch the sender or receiver "
            "of a case member within the window."
        ),
        input_schema={
            "type": "object",
            "properties": {"txn_id": _TXN_ID, "window_h": _WINDOW},
            "required": ["txn_id"],
        },
    ),
    ToolDefinition(
        name=SUBMIT,
        description=(
            "Give the final decision for the case. Code checks it. If it fails you get the "
            "reasons and one chance to correct it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": sorted(VERDICTS)},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "enum": sorted(CLAIM_CODES)},
                            "evidence_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["code", "evidence_ids"],
                    },
                },
                "note": {
                    "type": "string",
                    "maxLength": config.AGENT_NOTE_MAX_CHARS,
                    "description": "Optional. Must contain no digits or numbers spelled out as words.",
                },
            },
            "required": ["verdict", "confidence", "claims"],
        },
    ),
)


def tool_result_view(result: dict) -> str:
    """Turns a tool result into compact JSON for the model, without repeated IDs or edge dates."""

    def clean(value):
        if isinstance(value, dict):
            return {
                k: clean(v)
                for k, v in value.items()
                if k != "ids" and not (k == "txn_date" and "gap_minutes" in value)
            }
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return json.dumps(clean(result), separators=(",", ":"), default=str)


def seed_message(case: Case, seed: Alert, *, window_h: int) -> str:
    if len(case.member_txn_ids) > 1:
        case_line = (
            f"Case {case.case_id} groups {len(case.member_txn_ids)} top alerts that share an account "
            f"within {window_h} h. Members, best rank first: {', '.join(case.member_txn_ids)}."
        )
    else:
        case_line = "This is a single alert, not grouped with others."
    return (
        f"Triage this case. Day: {case.txn_date}. Default window: {window_h} h.\n"
        f"{case_line}\n"
        f"Representative alert: {case.representative_txn_id}. Its get_alert result:\n"
        f"{tool_result_view(seed)}\n"
        f"You have {config.AGENT_MAX_STEPS} steps. Fetch what you need, then call {SUBMIT}."
    )
