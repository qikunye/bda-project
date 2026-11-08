"""Native graphs and exact evidence resolution are pure presentation operations."""

from copy import deepcopy

from src.agent.sample_results import sample_investigations
from src.ui.components.agent_graph import (
    COLORS,
    build_network,
    graph_transactions,
    network_html,
)
from src.ui.components.evidence import resolve_evidence


def test_native_graph_has_exact_anchor_directions_ids_and_highlights():
    result = sample_investigations()["pass_through"]
    member = result["members"][0]
    before = deepcopy(member)
    network = build_network(member, ["txn_samplepassup"], "txn_samplepassdown1")
    edges = {edge["id"]: edge for edge in network.edges}
    alert = member["alert"]
    assert edges[alert["txn_id"]]["from"] == alert["sender"]
    assert edges[alert["txn_id"]]["to"] == alert["receiver"]
    assert edges["txn_samplepassup"]["to"] == alert["sender"]
    assert edges["txn_samplepassdown1"]["from"] == alert["receiver"]
    assert edges["txn_samplepassup"]["color"] == COLORS["cited"]
    assert edges["txn_samplepassdown1"]["color"] == COLORS["selected"]
    assert len(edges) == 4
    assert "Timestamp (UTC)" in edges["txn_samplepassup"]["title"]
    assert "Gap (minutes)" in edges["txn_samplepassup"]["title"]
    assert member == before


def test_graph_never_combines_parallel_transactions_or_currencies():
    member = sample_investigations()["pass_through"]["members"][0]
    extra = {**member["graph"]["upstream"][0], "txn_id": "txn_eur", "currency": "Euro", "amount": 10}
    member["graph"]["upstream"].append(extra)
    network = build_network(member)
    assert {edge["txn_id"] for edge in network.edges} == {
        "txn_samplepass", "txn_samplepassup", "txn_samplepassdown1", "txn_samplepassdown2", "txn_eur",
    }
    assert "Currency: Euro" in next(edge["title"] for edge in network.edges if edge["id"] == "txn_eur")


def test_empty_graph_and_missing_anchor_amount_do_not_fabricate_payments():
    member = sample_investigations()["no_graph"]["members"][0]
    assert graph_transactions(member) == []
    assert not build_network(member).edges
    member = sample_investigations()["pass_through"]["members"][0]
    member["alert"]["amount"] = None
    assert member["alert"]["txn_id"] not in {edge["id"] for edge in build_network(member).edges}


def test_tooltips_escape_html_and_graph_assets_are_offline():
    member = sample_investigations()["pass_through"]["members"][0]
    member["graph"]["upstream"][0]["payment_format"] = '<img src=x onerror="alert(123)">'
    member["graph"]["upstream"][0]["sender"] = "</script><script>evil()</script>"
    network = build_network(member)
    html = network_html(network)
    assert "<img src=x" not in next(edge["title"] for edge in network.edges if edge["id"] == "txn_samplepassup")
    assert "</script><script>evil()" not in html
    assert "<script src=" not in html and '<link rel="stylesheet"' not in html
    assert "new vis.Network" in html
    assert ".vis-tooltip" in html


def test_citations_resolve_selected_other_member_and_trace_only_records():
    result = sample_investigations()["fan_out_case"]
    own = resolve_evidence(result, "txn_samplefanup", "txn_samplefan")
    assert own.kind == "graph" and own.member_id == "txn_samplefan"
    other = resolve_evidence(result, "txn_samplefanmemberdown", "txn_samplefan")
    assert other.kind == "graph" and other.member_id == "txn_samplefanmember"
    for evidence_id in ("txn_samplefanout1", "txn_samplerelated"):
        reference = resolve_evidence(result, evidence_id)
        assert reference.kind == "trace" and reference.record["txn_id"] == evidence_id
        assert reference.tool in {"get_account_activity", "get_related_alerts"}


def test_unresolved_and_capped_evidence_never_select_a_different_transaction():
    result = sample_investigations()["pass_through"]
    reference = resolve_evidence(result, "txn_doesnotexist")
    assert reference.kind == "unresolved" and reference.record is None
    result["members"][0]["graph"]["edges_capped"] = True
    reference = resolve_evidence(result, "txn_omitted")
    assert reference.kind == "limited" and reference.record is None
