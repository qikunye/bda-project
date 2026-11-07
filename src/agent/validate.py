"""Checks the model's decision against what the tools returned in this run.
It rejects made-up IDs and numbers, and sets caveats and confidence from the tool flags.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import get_args

from src import config
from src.agent.claims import case_id_for
from src.agent.contracts import (
    Alert,
    CaseGraph,
    Claim,
    ClaimCode,
    Confidence,
    Decision,
    NeighbourScore,
    RelatedAlerts,
    Verdict,
)

VERDICTS = frozenset(get_args(Verdict))
CONFIDENCES = frozenset(get_args(Confidence))
CLAIM_CODES = frozenset(get_args(ClaimCode))
DECISION_FIELDS = frozenset(
    {"verdict", "confidence", "claims", "note"}
)
MAX_EVIDENCE_IDS_PER_CLAIM = 50

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    """Finds the numbers in a text, ignoring thousands commas ('12,345.6' becomes '12345.6')."""

    return {match.replace(",", "") for match in _NUMBER.findall(text)}


@dataclass
class RunEvidence:
    """Everything the model has been shown in one run, used to check its decision."""

    representative: str
    members: tuple[str, ...]
    top_k: int = config.AGENT_TOP_K
    # Each ID a tool result listed (transactions and accounts).
    known_ids: set[str] = field(default_factory=set)
    # Transaction ID -> date, for those the tools can be asked about. Starts with the members.
    txn_dates: dict[str, str] = field(default_factory=dict)
    alerts: dict[str, Alert] = field(default_factory=dict)
    graphs: dict[str, CaseGraph] = field(default_factory=dict)
    neighbours: dict[str, NeighbourScore] = field(default_factory=dict)
    # One entry per get_related_alerts call. The model may call it for several case members.
    related_calls: list[RelatedAlerts] = field(default_factory=list)
    # Digit runs in everything shown to the LLM, for the note check.
    numbers: set[str] = field(default_factory=set)

    def record(self, tool: str, result: Mapping, shown_text: str) -> None:
        """Stores one tool result that the model was shown."""

        self.known_ids.update(result.get("ids", ()))
        self.numbers |= numbers_in(shown_text)
        if tool == "get_alert" and result.get("found"):
            self.alerts[result["txn_id"]] = result
            self.txn_dates[result["txn_id"]] = result["txn_date"]
        elif tool == "get_case_graph":
            self.graphs[result["txn_id"]] = result
            for edge in result["upstream"] + result["downstream"]:
                self.txn_dates[edge["txn_id"]] = edge["txn_date"]
        elif tool == "get_neighbour_scores":
            for neighbour in result["neighbours"]:
                self.neighbours[neighbour["txn_id"]] = neighbour
        elif tool == "get_related_alerts":
            self.related_calls.append(result)
            for alert in result["alerts"]:
                self.txn_dates[alert["txn_id"]] = alert["txn_date"]

    @property
    def related(self) -> RelatedAlerts | None:
        """All related alerts the model has seen, merged across calls. None if never called."""

        if not self.related_calls:
            return None
        merged = {
            (alert["txn_id"], alert["account"]): alert
            for call in self.related_calls
            for alert in call["alerts"]
        }
        alerts = sorted(
            merged.values(),
            key=lambda a: (a["risk_rank_day"], a["txn_id"], a["account"]),
        )
        return RelatedAlerts(
            alerts=alerts,
            top_k=self.related_calls[0]["top_k"],
            window_h=max(call["window_h"] for call in self.related_calls),
            ids=list(
                dict.fromkeys(i for call in self.related_calls for i in call["ids"])
            ),
        )

    @property
    def edge_txn_ids(self) -> set[str]:
        return {
            edge["txn_id"]
            for graph in self.graphs.values()
            for edge in graph["upstream"] + graph["downstream"]
        }

    # Tool flags. Each is False until the tool that sets it has run.

    @property
    def no_graph(self) -> bool:
        """True if the representative's graph was fetched and has no payments."""

        graph = self.graphs.get(self.representative)
        return graph is not None and graph["no_graph"]

    @property
    def truncated(self) -> bool:
        return any(graph["window_truncated"] for graph in self.graphs.values())

    @property
    def no_reasons(self) -> bool:
        alert = self.alerts.get(self.representative)
        return alert is not None and alert["reasons"] is None

    @property
    def has_reasons(self) -> bool:
        alert = self.alerts.get(self.representative)
        return alert is not None and alert["reasons"] is not None


@dataclass(frozen=True)
class Validated:
    decision: Decision | None
    errors: tuple[str, ...] = ()
    # What code changed on an accepted decision, e.g. "added TRUNCATED_WINDOW".
    adjustments: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.decision is not None


def _string_list(value, name: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{name} must be a list of ID strings.")
        return []
    return value


def _parse_shape(payload) -> tuple[dict | None, list[str]]:
    """Checks the decision has the right fields and allowed values."""

    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return None, ["The decision must be a JSON object."]

    unknown = sorted(set(payload) - DECISION_FIELDS)
    if unknown:
        hint = (
            " case_id is set by code, so leave it out." if "case_id" in unknown else ""
        )
        errors.append(f"Unknown field(s): {unknown}.{hint}")

    verdict, confidence = payload.get("verdict"), payload.get("confidence")
    if verdict not in VERDICTS:
        errors.append(f"verdict must be one of {sorted(VERDICTS)}, got {verdict!r}.")
    if confidence not in CONFIDENCES:
        errors.append(
            f"confidence must be one of {sorted(CONFIDENCES)}, got {confidence!r}."
        )

    claims: list[Claim] = []
    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        errors.append("claims must be a list of {code, evidence_ids}.")
        raw_claims = []
    seen_codes: set[str] = set()
    for raw in raw_claims:
        if not isinstance(raw, Mapping) or set(raw) != {"code", "evidence_ids"}:
            errors.append(
                "Each claim must have exactly the fields code and evidence_ids."
            )
            continue
        code = raw["code"]
        if code not in CLAIM_CODES:
            errors.append(f"Unknown claim code {code!r}. Valid: {sorted(CLAIM_CODES)}.")
            continue
        if code in seen_codes:
            errors.append(f"Claim {code} appears twice. List each code once.")
            continue
        seen_codes.add(code)
        ids = _string_list(raw["evidence_ids"], f"{code} evidence_ids", errors)
        if not ids:
            errors.append(f"Claim {code} needs at least one evidence ID.")
        elif len(ids) > MAX_EVIDENCE_IDS_PER_CLAIM:
            errors.append(
                f"Claim {code} cites {len(ids)} IDs. The limit is {MAX_EVIDENCE_IDS_PER_CLAIM}."
            )
        claims.append(Claim(code=code, evidence_ids=list(dict.fromkeys(ids))))

    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        errors.append("note must be a string or null.")
        note = None
    note = (note or "").strip() or None

    if errors:
        return None, errors
    return (
        {
            "verdict": verdict,
            "confidence": confidence,
            "claims": claims,
            "note": note,
        },
        [],
    )


def _grounding_error(claim: Claim, evidence: RunEvidence) -> str | None:
    """Returns an error if a claim isn't backed by the tool results, otherwise None."""

    code, ids = claim["code"], set(claim["evidence_ids"])
    if code == "NEIGHBOURS_HIGH_RISK":
        high = {t for t, n in evidence.neighbours.items() if n["in_day_top_k"]}
        if not ids <= high:
            return (
                f"NEIGHBOURS_HIGH_RISK must cite only neighbours that get_neighbour_scores "
                f"shows inside the day's top {evidence.top_k}. Not so: {sorted(ids - high)[:5]}."
            )
    elif code == "FAST_CHAIN":
        if not ids <= evidence.edge_txn_ids:
            return (
                "FAST_CHAIN must cite only payments listed by get_case_graph. "
                f"Not so: {sorted(ids - evidence.edge_txn_ids)[:5]}."
            )
    elif code == "REPEAT_ACCOUNT_ALERTS":
        related = (
            {a["txn_id"] for a in evidence.related["alerts"]}
            if evidence.related
            else set()
        )
        if not ids <= related:
            return (
                "REPEAT_ACCOUNT_ALERTS must cite only alerts returned by get_related_alerts. "
                f"Not so: {sorted(ids - related)[:5]}."
            )
    elif code in ("REASONS_CORROBORATED", "REASONS_NOT_SUPPORTED"):
        if not evidence.has_reasons:
            return f"{code} needs the representative alert to have model reasons."
        if not evidence.graphs:
            return f"{code} needs a get_case_graph result first."
        if code == "REASONS_CORROBORATED" and all(
            g["no_graph"] for g in evidence.graphs.values()
        ):
            return (
                "REASONS_CORROBORATED cannot be claimed when the graph has no payments."
            )
    return None


def validate_decision(payload, evidence: RunEvidence) -> Validated:
    fields, errors = _parse_shape(payload)
    if fields is None:
        return Validated(None, tuple(errors))

    verdict, confidence = fields["verdict"], fields["confidence"]
    claims: list[Claim] = fields["claims"]
    note: str | None = fields["note"]
    codes = {c["code"] for c in claims}

    # 2. Every cited ID came from a tool result in this run.
    for claim in claims:
        unseen = [i for i in claim["evidence_ids"] if i not in evidence.known_ids]
        if unseen:
            errors.append(
                f"Claim {claim['code']} cites IDs no tool returned in this run: {unseen[:5]}."
            )
    # 3. No payments to check is not evidence that an alert is harmless.
    if (
        verdict == "dont_raise"
        and evidence.graphs
        and all(graph["no_graph"] for graph in evidence.graphs.values())
    ):
        errors.append(
            "dont_raise needs payments that fail to support the reasons, but the graphs fetched "
            "have none. Use not_sure, or fetch another case member's graph."
        )

    for claim in claims:
        problem = _grounding_error(claim, evidence)
        if problem:
            errors.append(problem)
    if {"REASONS_CORROBORATED", "REASONS_NOT_SUPPORTED"} <= codes:
        errors.append(
            "REASONS_CORROBORATED and REASONS_NOT_SUPPORTED contradict each other."
        )

    # 4. The note is short and holds no number the tools did not return.
    if note is not None:
        if len(note) > config.AGENT_NOTE_MAX_CHARS:
            errors.append(
                f"note is {len(note)} characters. The limit is {config.AGENT_NOTE_MAX_CHARS}."
            )
        invented = sorted(numbers_in(note) - evidence.numbers)
        if invented:
            errors.append(
                f"note contains numbers that no tool result shows: {invented[:5]}. "
                "Remove them, or write the note without numbers."
            )

    # 5. Caveat claims match the tool flags.
    flags: dict[str, bool] = {
        "NO_GRAPH_AVAILABLE": evidence.no_graph,
        "TRUNCATED_WINDOW": evidence.truncated,
        "NO_REASONS_AVAILABLE": evidence.no_reasons,
    }
    for code, flag in flags.items():
        if code in codes and not flag:
            errors.append(f"{code} was claimed, but the tool flags do not show it.")

    if errors:
        return Validated(None, tuple(errors))

    adjustments: list[str] = []
    for code, flag in flags.items():
        if flag and code not in codes:
            claims.append(Claim(code=code, evidence_ids=[evidence.representative]))
            adjustments.append(f"added {code}")
    if evidence.no_graph and confidence != "low":
        confidence = "low"
        adjustments.append("confidence lowered to low")
    elif evidence.truncated and confidence == "high":
        # A daily run is nearly always truncated, so this caps confidence instead of erasing it.
        confidence = "medium"
        adjustments.append("confidence lowered to medium")

    decision = Decision(
        verdict=verdict,
        confidence=confidence,
        case_id=case_id_for(evidence.representative)
        if len(evidence.members) > 1
        else None,
        claims=claims,
        note=note,
    )
    return Validated(decision, (), tuple(adjustments))
