"""Builds the parts of the brief that code writes: gaps, money flow, next-check rules and sentences.
Every number in a sentence comes from a tool result, never from the LLM."""

from typing import get_args

from src import config
from src.agent.contracts import (
    CaseMember,
    GapCode,
    MemberMoneyFlow,
    NextCheckCode,
    RenderedFinding,
    RenderedGap,
    RenderedNextCheck,
)
from src.agent.evidence import RunEvidence
from src.agent.findings import Instance, matching

GAP_CODES: tuple[str, ...] = get_args(GapCode)
NEXT_CHECK_CODES: tuple[str, ...] = get_args(NextCheckCode)

# A next check is allowed only alongside one of these finding or gap codes.
NEXT_CHECK_NEEDS: dict[str, frozenset[str]] = {
    "CHECK_ONWARD_ACCOUNTS": frozenset({"PASS_THROUGH", "FAN_OUT"}),
    "CHECK_SOURCE_ACCOUNTS": frozenset({"PASS_THROUGH", "FAN_IN"}),
    "CHECK_RETURN_FLOW": frozenset({"MONEY_RETURNS"}),
    "REVIEW_RELATED_ALERTS": frozenset({"REPEAT_ACCOUNT_ALERTS", "NEIGHBOURS_HIGH_RISK"}),
    "CHECK_ACCOUNT_HISTORY": frozenset({"SHORT_HISTORY"}),
    "RECHECK_AFTER_MORE_DATA": frozenset({"SHORT_OBSERVATION", "TRUNCATED_WINDOW"}),
}
assert tuple(NEXT_CHECK_NEEDS) == NEXT_CHECK_CODES

NEXT_CHECK_SENTENCES: dict[str, str] = {
    "CHECK_ONWARD_ACCOUNTS": "Look at where the money went next.",
    "CHECK_SOURCE_ACCOUNTS": "Look at where the money came from.",
    "CHECK_RETURN_FLOW": "Look at the money that came back.",
    "REVIEW_RELATED_ALERTS": "Open the other alerts on these accounts.",
    "CHECK_ACCOUNT_HISTORY": "Look at the account's earlier activity.",
    "RECHECK_AFTER_MORE_DATA": "Come back to this alert once more days of data are loaded.",
}

_FINDING_LABELS: dict[str, str] = {
    "PASS_THROUGH": "money passing through",
    "FAN_OUT": "one account paying many others",
    "FAN_IN": "one account paid by many others",
    "MONEY_RETURNS": "money returning to its source",
    "NEIGHBOURS_HIGH_RISK": "high-ranked payments nearby",
    "REPEAT_ACCOUNT_ALERTS": "other alerts on the same accounts",
}


def case_id_for(representative_txn_id: str) -> str:
    return f"case_{representative_txn_id}"


def allowed_next_checks(
    finding_codes: set[str] | frozenset[str], gap_codes: set[str] | frozenset[str]
) -> list[str]:
    """Every next check that the findings and gaps allow, in fixed order."""

    present = set(finding_codes) | set(gap_codes)
    return [code for code in NEXT_CHECK_CODES if NEXT_CHECK_NEEDS[code] & present]


def render_next_checks(codes) -> list[RenderedNextCheck]:
    return [
        RenderedNextCheck(code=code, sentence=NEXT_CHECK_SENTENCES[code])
        for code in NEXT_CHECK_CODES
        if code in set(codes)
    ]


def render_findings(
    findings: list[tuple[str, list[str]]], instances: list[Instance]
) -> list[RenderedFinding]:
    """The displayed sentence for each finding, from the first place its rule fired."""

    rendered: list[RenderedFinding] = []
    for code, evidence_ids in findings:
        found = matching(code, set(evidence_ids), instances)
        if not found:
            raise ValueError(f"{code} has no supporting tool result to render.")
        sentence = found[0].sentence
        if len(found) > 1:
            sentence += f" Seen {len(found)} times in this case."
        rendered.append(
            RenderedFinding(code=code, sentence=sentence, evidence_ids=list(evidence_ids))
        )
    return rendered


def compute_gaps(evidence: RunEvidence) -> list[RenderedGap]:
    """What the tools could not see or check, set from tool flags only."""

    graphs = evidence.graphs_in_case_order()
    activities = evidence.activities_in_case_order()
    gaps: dict[str, str] = {}

    lead = evidence.graphs.get(evidence.representative)
    if lead is not None and lead["no_graph"]:
        gaps["NO_GRAPH_AVAILABLE"] = (
            f"No payments were found in the {lead['window_h']} h window around this alert."
        )

    truncated = next((g for g in [*graphs, *activities] if g["window_truncated"]), None)
    if truncated is not None:
        gaps["TRUNCATED_WINDOW"] = (
            f"The {truncated['window_h']} h window runs past the end of the data "
            f"({config.AGENT_DATA_HORIZON}), so later payments can never be seen."
        )

    observed = [g["hours_observed_after_alert"] for g in graphs] or [
        a["hours_observed_after_alert"] for a in activities
    ]
    if observed and min(observed) < config.AGENT_MIN_HOURS_OBSERVED:
        gaps["SHORT_OBSERVATION"] = (
            f"Only {min(observed):g} h of data after the alert are visible, and at least "
            f"{config.AGENT_MIN_HOURS_OBSERVED:g} h are needed to say money did not move on."
        )

    if not activities:
        gaps["SHORT_HISTORY"] = "The accounts' earlier activity was not checked."
    else:
        days = min(a["history_days"] for a in activities)
        if days < config.AGENT_MIN_HISTORY_DAYS:
            gaps["SHORT_HISTORY"] = (
                f"Only {days} earlier days of account history are available, and at least "
                f"{config.AGENT_MIN_HISTORY_DAYS} are needed to spot a change."
            )

    return [RenderedGap(code=code, sentence=gaps[code]) for code in GAP_CODES if code in gaps]


def build_money_flow(members: list[CaseMember]) -> list[MemberMoneyFlow]:
    """One entry per case member, from its graph. Code only: the LLM never writes these numbers."""

    return [
        MemberMoneyFlow(
            txn_id=member["graph"]["txn_id"],
            no_graph=member["graph"]["no_graph"],
            n_upstream=member["graph"]["n_upstream"],
            n_downstream=member["graph"]["n_downstream"],
            per_currency=member["graph"]["flow"],
            hours_observed_after_alert=member["graph"]["hours_observed_after_alert"],
        )
        for member in members
    ]


def template_summary(finding_codes: list[str]) -> str:
    """The summary when code alone builds the brief."""

    if not finding_codes:
        return "The fixed rules found no pattern around this alert."
    return "The fixed rules found: " + ", ".join(_FINDING_LABELS[c] for c in finding_codes) + "."
