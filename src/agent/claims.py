"""Turn claim codes into display sentences using tool results (PLAN.md 6.3).

The LLM picks a claim code and cites IDs. Every number shown here comes from a tool result, so
the LLM never writes a displayed number. If a tool did not run, the sentence has no numbers.
"""

from collections.abc import Callable, Iterable

from src import config
from src.agent.contracts import (
    CaseGraph,
    Claim,
    ClaimCode,
    Decision,
    NeighbourScores,
    RelatedAlerts,
    RenderedClaim,
)


def _corroborated(claim, graph, neighbours, related, graphs) -> str:
    if graph is None:
        return "The payment graph is consistent with the model's reasons."
    return (
        "The payment graph is consistent with the model's reasons: "
        f"{graph['n_upstream']} payments in and {graph['n_downstream']} payments out "
        f"within {graph['window_h']} h."
    )


def _neighbours_high_risk(claim, graph, neighbours, related, graphs) -> str:
    if neighbours is None:
        return "Neighbouring transactions also rank high in the risk model."
    scored = [n for n in neighbours["neighbours"] if n["scored"]]
    high = [n for n in scored if n["in_day_top_k"]]
    return (
        f"Neighbours in the day's top {config.AGENT_TOP_K}: "
        f"{len(high)} of {len(scored)} scored."
    )


def _fast_chain(claim, graph, neighbours, related, graphs) -> str:
    cited = set(claim["evidence_ids"])
    gaps = [
        edge["gap_minutes"]
        for shown in graphs
        for edge in shown["upstream"] + shown["downstream"]
        if edge["txn_id"] in cited
    ]
    if gaps:
        return f"Shortest gap between an alert and a cited payment: {min(gaps)} minutes."
    return "Cited payments happened soon after or before the alert."


def _repeat_alerts(claim, graph, neighbours, related, graphs) -> str:
    if related is None:
        return "Other top alerts touch the same accounts."
    return (
        f"Other top {related['top_k']} alerts on the same accounts within "
        f"{related['window_h']} h: {len({a['txn_id'] for a in related['alerts']})}."
    )


def _not_supported(claim, graph, neighbours, related, graphs) -> str:
    return "The payment graph shows nothing that supports the model's reasons."


def _no_graph(claim, graph, neighbours, related, graphs) -> str:
    if graph is None:
        return "No payments were found around this alert."
    return (
        f"No payments were found in the {graph['window_h']} h window around this alert."
    )


def _truncated(claim, graph, neighbours, related, graphs) -> str:
    if graph is None:
        return "The downstream window runs past the data available, so later payments are unseen."
    return (
        f"The {graph['window_h']} h downstream window runs past the data available "
        f"({graph['horizon_end']}), so later payments are unseen."
    )


def _no_reasons(claim, graph, neighbours, related, graphs) -> str:
    return "No model reasons are available for this alert."


_RENDERERS: dict[ClaimCode, Callable[..., str]] = {
    "REASONS_CORROBORATED": _corroborated,
    "NEIGHBOURS_HIGH_RISK": _neighbours_high_risk,
    "FAST_CHAIN": _fast_chain,
    "REPEAT_ACCOUNT_ALERTS": _repeat_alerts,
    "REASONS_NOT_SUPPORTED": _not_supported,
    "NO_GRAPH_AVAILABLE": _no_graph,
    "TRUNCATED_WINDOW": _truncated,
    "NO_REASONS_AVAILABLE": _no_reasons,
}


def render_claim(
    claim: Claim,
    *,
    graph: CaseGraph | None = None,
    neighbours: NeighbourScores | None = None,
    related: RelatedAlerts | None = None,
    member_graphs: Iterable[CaseGraph] = (),
) -> RenderedClaim:
    graphs = ([graph] if graph is not None else []) + list(member_graphs)
    sentence = _RENDERERS[claim["code"]](claim, graph, neighbours, related, graphs)
    return RenderedClaim(
        code=claim["code"], sentence=sentence, evidence_ids=list(claim["evidence_ids"])
    )


def render_claims(
    decision: Decision,
    *,
    graph: CaseGraph | None = None,
    neighbours: NeighbourScores | None = None,
    related: RelatedAlerts | None = None,
    member_graphs: Iterable[CaseGraph] = (),
) -> list[RenderedClaim]:
    member_graphs = list(member_graphs)
    return [
        render_claim(
            claim,
            graph=graph,
            neighbours=neighbours,
            related=related,
            member_graphs=member_graphs,
        )
        for claim in decision["claims"]
    ]


def case_id_for(representative_txn_id: str) -> str:
    return f"case_{representative_txn_id}"
