"""The fixed rules that decide whether each finding (such as pass-through or fan-out) applies.
They drive the code-only brief, the validator's checks and the displayed sentences."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import get_args

from src import config
from src.agent.contracts import FindingCode
from src.agent.evidence import RunEvidence

FINDING_CODES: tuple[str, ...] = get_args(FindingCode)

# What each finding needs. Shown to the LLM when it submits one that the results do not support.
REQUIREMENTS: dict[str, str] = {
    "PASS_THROUGH": (
        "get_case_graph must show money coming in and going on within "
        f"{config.AGENT_PASS_THROUGH_MAX_GAP_MIN} minutes of the alert, with onward payments of at "
        f"least {round(config.AGENT_PASS_THROUGH_MIN_ONWARD_SHARE * 100)}% of the alert amount, in "
        "the alert's currency. Cite one payment in and the first payment out."
    ),
    "FAN_OUT": (
        "get_account_activity with side=sender must show payments to at least "
        f"{config.AGENT_FAN_MIN_COUNTERPARTIES} different accounts. Cite payments it lists."
    ),
    "FAN_IN": (
        "get_account_activity with side=receiver must show payments from at least "
        f"{config.AGENT_FAN_MIN_COUNTERPARTIES} different accounts. Cite payments it lists."
    ),
    "MONEY_RETURNS": (
        "get_case_graph must list a returned_to_source entry. Cite both its downstream_txn_id "
        "and its source_txn_id."
    ),
    "NEIGHBOURS_HIGH_RISK": (
        f"get_neighbour_scores must show at least {config.AGENT_NEIGHBOURS_HIGH_MIN} payment(s) "
        f"outside the case in the day's top {config.AGENT_TOP_K}. Cite those."
    ),
    "REPEAT_ACCOUNT_ALERTS": "get_related_alerts must return alerts. Cite them.",
    "ACTIVITY_SPIKE": (
        "get_account_activity must show at least "
        f"{config.AGENT_SPIKE_MIN_PAYMENTS} payments in the window, a daily rate "
        f"{config.AGENT_SPIKE_FACTOR:g} times the earlier daily mean or more, with at least "
        f"{config.AGENT_MIN_HISTORY_DAYS} earlier days. Cite payments it lists."
    ),
}


@dataclass(frozen=True)
class Instance:
    """One place where a rule fired.

    ``ids`` are the payments or alerts that may be cited for it, the most important first. A finding
    must cite everything in ``core`` (which may be empty) and nothing outside the ids.
    """

    code: str
    member: str  # The case member whose results fired it, or "" when it covers the whole case.
    ids: tuple[str, ...]
    core: frozenset[str]
    sentence: str


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v for v in values if v))


def _capped(ids: tuple[str, ...]) -> tuple[str, ...]:
    return ids[: config.AGENT_MAX_EVIDENCE_IDS]


def _percent(share: float) -> int:
    return round(share * 100)


def _pass_through(evidence: RunEvidence) -> list[Instance]:
    found: list[Instance] = []
    for graph in evidence.graphs_in_case_order():
        for currency, flow in graph["flow"].items():
            gap, share = flow["fastest_onward_gap_minutes"], flow["onward_share"]
            down_id = flow["fastest_onward_txn_id"]
            if gap is None or share is None or down_id is None:
                continue
            if (
                gap > config.AGENT_PASS_THROUGH_MAX_GAP_MIN
                or share < config.AGENT_PASS_THROUGH_MIN_ONWARD_SHARE
            ):
                continue
            up = next((e for e in graph["upstream"] if e["currency"] == currency), None)
            if up is None:
                continue
            same_currency = [
                e["txn_id"]
                for e in graph["upstream"] + graph["downstream"]
                if e["currency"] == currency
            ]
            found.append(
                Instance(
                    code="PASS_THROUGH",
                    member=graph["txn_id"],
                    ids=_capped(_unique([down_id, up["txn_id"], *same_currency])),
                    core=frozenset({down_id, up["txn_id"]}),
                    sentence=(
                        f"Money passed through: in {currency}, payments out of the receiver came "
                        f"to {_percent(share)}% of the alert amount, the first {gap} minutes after "
                        "the alert."
                    ),
                )
            )
    return found


def _fan(evidence: RunEvidence, code: str, side: str) -> list[Instance]:
    outgoing = side == "sender"
    found: list[Instance] = []
    for activity in evidence.activities_in_case_order():
        if activity["side"] != side:
            continue
        count = activity["n_outgoing" if outgoing else "n_incoming"]
        counterparties = activity[
            "n_outgoing_counterparties" if outgoing else "n_incoming_counterparties"
        ]
        if counterparties < config.AGENT_FAN_MIN_COUNTERPARTIES:
            continue
        payments = activity["outgoing" if outgoing else "incoming"]
        who, verb, prep = (
            ("sender", "paid", "to") if outgoing else ("receiver", "was paid by", "from")
        )
        found.append(
            Instance(
                code=code,
                member=activity["txn_id"],
                ids=_capped(_unique(p["txn_id"] for p in payments)),
                core=frozenset(),
                sentence=(
                    f"The {who} {verb} {counterparties} different accounts ({count} payments) "
                    f"within {activity['window_h']} h either side of the alert."
                ),
            )
        )
    return found


def _money_returns(evidence: RunEvidence) -> list[Instance]:
    found: list[Instance] = []
    for graph in evidence.graphs_in_case_order():
        for entry in graph["returned_to_source"]:
            target = (
                "the alert's sender"
                if entry["kind"] == "alert_sender"
                else "an account that had paid the alert's sender"
            )
            found.append(
                Instance(
                    code="MONEY_RETURNS",
                    member=graph["txn_id"],
                    ids=_unique([entry["downstream_txn_id"], entry["source_txn_id"]]),
                    core=frozenset({entry["downstream_txn_id"], entry["source_txn_id"]}),
                    sentence=(
                        f"Money went back to {target}, {entry['gap_minutes']} minutes "
                        "after the alert."
                    ),
                )
            )
    return found


def _neighbours_high_risk(evidence: RunEvidence) -> list[Instance]:
    members = set(evidence.members)
    scored = [
        n
        for n in evidence.neighbours.values()
        if n["scored"] and n["txn_id"] not in members
    ]
    high = sorted(
        (n for n in scored if n["in_day_top_k"]),
        key=lambda n: (n["risk_rank_day"], n["txn_id"]),
    )
    if len(high) < config.AGENT_NEIGHBOURS_HIGH_MIN:
        return []
    return [
        Instance(
            code="NEIGHBOURS_HIGH_RISK",
            member="",
            ids=_capped(tuple(n["txn_id"] for n in high)),
            core=frozenset(),
            sentence=(
                f"Scored payments around the case that are in the day's top {evidence.top_k}: "
                f"{len(high)} of {len(scored)}."
            ),
        )
    ]


def _repeat_account_alerts(evidence: RunEvidence) -> list[Instance]:
    related = evidence.related
    # The case's own members share these accounts by construction, so they are not repeats.
    outside = [a for a in (related["alerts"] if related else []) if a["txn_id"] not in evidence.members]
    if not outside:
        return []
    txn_ids = _unique(a["txn_id"] for a in outside)  # Already best rank first.
    accounts = {a["account"] for a in outside}
    return [
        Instance(
            code="REPEAT_ACCOUNT_ALERTS",
            member="",
            ids=_capped(txn_ids),
            core=frozenset(),
            sentence=(
                f"Other alerts in the day's top {related['top_k']} on accounts in this case "
                f"within {related['window_h']} h: {len(txn_ids)}, on {len(accounts)} account(s)."
            ),
        )
    ]


def _activity_spike(evidence: RunEvidence) -> list[Instance]:
    found: list[Instance] = []
    for activity in evidence.activities_in_case_order():
        history = activity["daily_history"]
        if activity["history_days"] < config.AGENT_MIN_HISTORY_DAYS:
            continue
        in_window = activity["n_incoming"] + activity["n_outgoing"]
        if in_window < config.AGENT_SPIKE_MIN_PAYMENTS:
            continue
        # Days with no payments have no entry in daily_history, so divide by the days covered.
        daily_mean = (
            sum(d["n_incoming"] + d["n_outgoing"] for d in history)
            / activity["history_days"]
        )
        observed_h = activity["window_h"] + min(
            activity["window_h"], activity["hours_observed_after_alert"]
        )
        if observed_h <= 0:
            continue
        daily_rate = in_window / (observed_h / 24)
        if daily_rate < config.AGENT_SPIKE_FACTOR * daily_mean:
            continue
        compared = (
            f"about {daily_rate / daily_mean:.1f} times its earlier daily mean of "
            f"{daily_mean:.1f}"
            if daily_mean > 0
            else "with no payments on the earlier days"
        )
        found.append(
            Instance(
                code="ACTIVITY_SPIKE",
                member=activity["txn_id"],
                ids=_capped(
                    _unique(p["txn_id"] for p in activity["incoming"] + activity["outgoing"])
                ),
                core=frozenset(),
                sentence=(
                    f"The {activity['side']}'s account made {in_window} other payments around "
                    f"the alert, {compared}."
                ),
            )
        )
    return found


_DETECTORS: dict[str, Callable[[RunEvidence], list[Instance]]] = {
    "PASS_THROUGH": _pass_through,
    "FAN_OUT": lambda e: _fan(e, "FAN_OUT", "sender"),
    "FAN_IN": lambda e: _fan(e, "FAN_IN", "receiver"),
    "MONEY_RETURNS": _money_returns,
    "NEIGHBOURS_HIGH_RISK": _neighbours_high_risk,
    "REPEAT_ACCOUNT_ALERTS": _repeat_account_alerts,
    "ACTIVITY_SPIKE": _activity_spike,
}
assert tuple(_DETECTORS) == FINDING_CODES


def detect(evidence: RunEvidence) -> list[Instance]:
    """Every place a rule fires on the tool results so far, in finding-code order."""

    return [i for code in FINDING_CODES for i in _DETECTORS[code](evidence)]


def matching(code: str, cited: set[str], instances: list[Instance]) -> list[Instance]:
    """The instances of ``code`` that a finding citing ``cited`` is a valid claim about."""

    return [
        i
        for i in instances
        if i.code == code and i.core <= cited and cited & set(i.ids)
    ]


def check_finding(code: str, cited: set[str], instances: list[Instance]) -> str | None:
    """Why a finding is not supported by the tool results, or None if it is."""

    of_code = [i for i in instances if i.code == code]
    if not of_code:
        return f"{code} is not supported by the tool results so far. {REQUIREMENTS[code]}"
    allowed = {i for inst in of_code for i in inst.ids}
    outside = sorted(cited - allowed)
    if outside:
        return (
            f"{code} cites IDs the tool results do not show it with: {outside[:5]}. "
            f"{REQUIREMENTS[code]}"
        )
    if not matching(code, cited, instances):
        return f"{code} is missing required evidence. {REQUIREMENTS[code]}"
    return None


def s1_findings(instances: list[Instance]) -> list[tuple[str, list[str]]]:
    """For the code-only brief: each code that fired, with up to AGENT_S1_MAX_EVIDENCE cited IDs.

    The first place it fired comes first, so the cited IDs always include its core.
    """

    result: list[tuple[str, list[str]]] = []
    for code in FINDING_CODES:
        of_code = [i for i in instances if i.code == code]
        if not of_code:
            continue
        ids = list(_unique(i for inst in of_code for i in inst.ids))
        first_core = [i for i in of_code[0].ids if i in of_code[0].core]
        ordered = list(dict.fromkeys(first_core + ids))
        result.append((code, ordered[: config.AGENT_S1_MAX_EVIDENCE]))
    return result


__all__ = [
    "FINDING_CODES",
    "Instance",
    "REQUIREMENTS",
    "check_finding",
    "detect",
    "matching",
    "s1_findings",
]
