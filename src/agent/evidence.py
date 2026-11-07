"""Records what the tools returned in one run, so the rules, the validator and the code-only
brief all see the same facts."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from src import config
from src.agent.contracts import (
    AccountActivity,
    Alert,
    CaseGraph,
    NeighbourScore,
    RelatedAlerts,
)


@dataclass
class RunEvidence:
    """Everything the model has been shown in one run."""

    representative: str
    members: tuple[str, ...]
    top_k: int = config.AGENT_TOP_K
    # Each ID a tool result listed (transactions and accounts).
    known_ids: set[str] = field(default_factory=set)
    # Transaction ID -> date, for those the tools can be asked about. Starts with the members.
    txn_dates: dict[str, str] = field(default_factory=dict)
    alerts: dict[str, Alert] = field(default_factory=dict)
    graphs: dict[str, CaseGraph] = field(default_factory=dict)
    # Keyed by (alert txn ID, "sender" or "receiver").
    activities: dict[tuple[str, str], AccountActivity] = field(default_factory=dict)
    neighbours: dict[str, NeighbourScore] = field(default_factory=dict)
    # One entry per get_related_alerts call. The model may call it for several case members.
    related_calls: list[RelatedAlerts] = field(default_factory=list)

    def record(self, tool: str, result: Mapping) -> None:
        """Stores one tool result that the model was shown."""

        self.known_ids.update(result.get("ids", ()))
        if tool == "get_alert" and result.get("found"):
            self.alerts[result["txn_id"]] = result
            self.txn_dates[result["txn_id"]] = result["txn_date"]
        elif tool == "get_case_graph":
            self.graphs[result["txn_id"]] = result
            for edge in result["upstream"] + result["downstream"]:
                self.txn_dates[edge["txn_id"]] = edge["txn_date"]
        elif tool == "get_account_activity":
            self.activities[(result["txn_id"], result["side"])] = result
            for payment in result["incoming"] + result["outgoing"]:
                self.txn_dates[payment["txn_id"]] = payment["txn_date"]
        elif tool == "get_neighbour_scores":
            for neighbour in result["neighbours"]:
                self.neighbours[neighbour["txn_id"]] = neighbour
        elif tool == "get_related_alerts":
            self.related_calls.append(result)
            for alert in result["alerts"]:
                self.txn_dates[alert["txn_id"]] = alert["txn_date"]

    def graphs_in_case_order(self) -> list[CaseGraph]:
        return [self.graphs[m] for m in self.members if m in self.graphs]

    def activities_in_case_order(self) -> list[AccountActivity]:
        return [
            self.activities[(m, side)]
            for m in self.members
            for side in ("sender", "receiver")
            if (m, side) in self.activities
        ]

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
