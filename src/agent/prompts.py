"""Holds the agent's instructions, tool definitions and the text shown to the LLM. None of it may
name a label column.
"""

import json

from src import config
from src.agent.brief import NEXT_CHECK_CODES, NEXT_CHECK_NEEDS
from src.agent.cases import Case
from src.agent.contracts import Alert
from src.agent.findings import FINDING_CODES, REQUIREMENTS
from src.agent.llm import ToolDefinition

SUBMIT = "submit_brief"

_FINDING_HELP = "\n".join(f"  - {code}: {REQUIREMENTS[code]}" for code in FINDING_CODES)
_NEXT_CHECK_HELP = "\n".join(
    f"  - {code}: only with {' or '.join(sorted(NEXT_CHECK_NEEDS[code]))}"
    for code in NEXT_CHECK_CODES
)
_GAP_HELP = f"""\
  - NO_GRAPH_AVAILABLE: the representative's get_case_graph has no_graph true.
  - TRUNCATED_WINDOW: a graph or activity result has window_truncated true.
  - SHORT_OBSERVATION: hours_observed_after_alert is below {config.AGENT_MIN_HOURS_OBSERVED:g}.
  - SHORT_HISTORY: you did not call get_account_activity, or history_days is below {config.AGENT_MIN_HISTORY_DAYS}."""

SYSTEM_PROMPT = f"""\
You are an investigation assistant for an anti-money-laundering analyst team. A risk model has \
already ranked the day's transactions, and the analyst sees them in that order. You do not decide \
whether an alert is worth raising. You gather the payment evidence around one alert, and the small \
case of alerts it belongs to, and write a short brief so the analyst need not look it up by hand. \
You do not make accusations.

What you have
- Day rank 1 is the riskiest transaction of the day. The day's top {config.AGENT_TOP_K} are the queue. \
You are not shown why the model scored an alert.
- Read-only tools over the payment data. Upstream payments are payments into the alert's sender before \
the alert. Downstream payments are payments out of its receiver after it. Account activity is the \
sender's or receiver's other payments, in and out, around the alert, plus its daily counts on earlier days.
- You know nothing about which transactions are really illicit, and you must not guess. \
The data is synthetic.

Rules
- Use only tool results and the case facts in the first message. Use no outside knowledge.
- Cite only IDs that tool results returned. Code checks every finding against fixed rules and rejects \
the brief if a finding is not supported or an ID was not returned.
- Write no numbers of your own. Code builds every displayed sentence, the money flow and the gaps from \
tool results. Your summary is one short sentence of at most {config.AGENT_SUMMARY_MAX_CHARS} characters \
with no digits and no numbers spelled out as words, so keep amounts, counts, ranks, times and IDs out of it.
- Never add amounts across currencies.
- You have at most {config.AGENT_MAX_STEPS} steps. A step is one reply of yours, and it may contain \
several independent tool calls. Your last step must be {SUBMIT}.
- Usual plan: get_case_graph for the representative, get_account_activity for its sender and receiver, \
get_neighbour_scores for the payments the graph listed, get_related_alerts, then submit. Fetch other case \
members' graphs when the representative's has no payments or the case is small enough to cover.

The brief
- findings: patterns the results show. Pick only the ones that are true, and cite the IDs that show them. \
An empty list is a fine answer: it means no pattern was found, not that the alert is harmless.
{_FINDING_HELP}
- gaps: code sets these from the tool results. You never write them, but they decide which next checks \
you may pick, so call the tools that avoid a gap when you can (for example, get_account_activity avoids \
SHORT_HISTORY).
{_GAP_HELP}
- next_checks: what the analyst should look at next. Pick only ones that fit your findings or the gaps \
above.
{_NEXT_CHECK_HELP}
- summary: one short sentence that sums up the evidence and says nothing the findings do not.
- Do not write gaps, money flow or case_id in the brief. Code adds them.
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
            "Score, day rank and payment details of one transaction. The representative alert "
            "is already given in the first message. Use this for other case members or for "
            "neighbours the graph listed."
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
            "(downstream) within the window, for a case member. Gives counts, totals per currency, "
            "and flow per currency: the share of the money paid in that went on, and the fastest and "
            "median gap in minutes from the alert to an onward payment. returned_to_source lists "
            "onward payments that went back to the alert's sender or to an account that had paid "
            "it. hours_observed_after_alert says how much time after the alert the data covers: no "
            "onward payment only means something if it is large. Flags: no_graph (no payments "
            "found), window_truncated (the window runs past the data available to this review), "
            "edges_capped (only the first "
            f"{config.AGENT_MAX_EDGES_PER_DIRECTION} payments per direction are listed)."
        ),
        input_schema={
            "type": "object",
            "properties": {"txn_id": _TXN_ID, "window_h": _WINDOW},
            "required": ["txn_id"],
        },
    ),
    ToolDefinition(
        name="get_account_activity",
        description=(
            "The other payments of a case member's sender or receiver, in and out, within the window "
            "either side of the alert (the alert itself is left out). Gives counts, distinct "
            "counterparties and totals per currency, the nearest "
            f"{config.AGENT_MAX_EDGES_PER_DIRECTION} payments per direction, and daily counts and "
            "totals for the whole days before the alert's day (history_days says how many days). "
            "Use it to see whether an account pays many accounts, is paid by many, or is busier than "
            "usual."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "txn_id": _TXN_ID,
                "side": {
                    "type": "string",
                    "enum": ["sender", "receiver"],
                    "description": "Whose payments: the alert's sender or its receiver.",
                },
                "window_h": _WINDOW,
            },
            "required": ["txn_id", "side"],
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
            "of a case member within the window. Pass accounts to include more accounts, such as "
            "counterparties that a graph or activity result listed "
            f"(at most {config.AGENT_MAX_RELATED_ACCOUNTS} in all)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "txn_id": _TXN_ID,
                "window_h": _WINDOW,
                "accounts": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "description": "An account ID returned by a tool.",
                    },
                    "maxItems": config.AGENT_MAX_RELATED_ACCOUNTS - 2,
                },
            },
            "required": ["txn_id"],
        },
    ),
    ToolDefinition(
        name=SUBMIT,
        description=(
            "Give the final brief for the case. Code checks it. If it fails you get the "
            "reasons and one chance to correct it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "enum": list(FINDING_CODES)},
                            "evidence_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": config.AGENT_MAX_EVIDENCE_IDS,
                            },
                        },
                        "required": ["code", "evidence_ids"],
                    },
                },
                "next_checks": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(NEXT_CHECK_CODES)},
                },
                "summary": {
                    "type": "string",
                    "maxLength": config.AGENT_SUMMARY_MAX_CHARS,
                    "description": "One short sentence. Must contain no digits or numbers spelled out as words.",
                },
            },
            "required": ["findings", "next_checks", "summary"],
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
        f"Investigate this case. Day: {case.txn_date}. Default window: {window_h} h.\n"
        f"{case_line}\n"
        f"Representative alert: {case.representative_txn_id}. Its get_alert result:\n"
        f"{tool_result_view(seed)}\n"
        f"You have {config.AGENT_MAX_STEPS} steps. Fetch what you need, then call {SUBMIT}."
    )
