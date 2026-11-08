"""Native agent graph presentation; no legacy transforms or additional data reads."""

import json
from functools import lru_cache
from html import escape
from pathlib import Path
from textwrap import fill

import pyvis
import streamlit as st
from pyvis.network import Network

COLORS = {"sender": "#ed9223", "receiver": "#dc4455", "upstream": "#16877b",
          "downstream": "#337ac1", "cited": "#8054b3", "selected": "#a85c00"}


def graph_transactions(member: dict) -> list[dict]:
    """Keep each payment and currency distinct, including the anchor when available."""
    alert, graph = member["alert"], member["graph"]
    payments = []
    if alert.get("amount") is not None and alert.get("currency") and alert.get("payment_format"):
        payments.append({**alert, "gap_minutes": 0, "role": "ALERT"})
    for side in ("upstream", "downstream"):
        payments.extend({**edge, "role": side.upper()} for edge in graph[side])
    return list({payment["txn_id"]: payment for payment in payments}.values())


def build_network(member: dict, cited_ids=(), selected_id: str | None = None) -> Network:
    alert = member["alert"]
    network = Network(height="420px", width="100%", directed=True, cdn_resources="in_line")
    roles: dict[str, set[str]] = {
        alert["sender"]: {"sender"}, alert["receiver"]: {"receiver"},
    }
    if alert["sender"] == alert["receiver"]:
        roles[alert["sender"]] = {"sender", "receiver"}
    for side, field in (("upstream", "sender"), ("downstream", "receiver")):
        for edge in member["graph"][side]:
            roles.setdefault(edge[field], set()).add(side)
    grouped = {role: sorted(account for account, account_roles in roles.items()
                            if next(r for r in ("sender", "receiver", "upstream", "downstream")
                                    if r in account_roles) == role)
               for role in ("sender", "receiver", "upstream", "downstream")}
    for account, account_roles in roles.items():
        role = next(role for role in ("sender", "receiver", "upstream", "downstream") if role in account_roles)
        network.add_node(
            account, label=fill(account.replace("|", "|\n"), width=18), title=escape(account) + "<br>" + escape(", ".join(sorted(account_roles))),
            color=COLORS[role], size=26 if role in {"sender", "receiver"} else 18,
            x={"upstream": -380, "sender": -120, "receiver": 120, "downstream": 380}[role]
            + (grouped[role].index(account) // 8) * (-220 if role == "upstream" else 220),
            y=((grouped[role].index(account) % 8)
               - (min(8, len(grouped[role]) - (grouped[role].index(account) // 8) * 8) - 1) / 2) * 110,
            font={"size": 22, "color": "#172331", "strokeWidth": 4, "strokeColor": "#fafbfd"},
            borderWidth=2,
        )
    for payment in graph_transactions(member):
        txn_id = payment["txn_id"]
        details = {
            "Transaction": txn_id, "Role": payment["role"], "Timestamp (UTC)": payment["ts"],
            "From": payment["sender"], "To": payment["receiver"], "Amount": payment["amount"],
            "Currency": payment["currency"], "Payment format": payment["payment_format"],
            "Gap (minutes)": payment["gap_minutes"],
        }
        selected = txn_id == selected_id
        color = COLORS["selected"] if selected else (
            COLORS["cited"] if txn_id in cited_ids else "#627387"
        )
        network.add_edge(
            payment["sender"], payment["receiver"], id=txn_id, txn_id=txn_id,
            title="<br>".join(f"{escape(key)}: {escape(str(value))}" for key, value in details.items()),
            color=color, width=6 if selected else (4 if payment["role"] == "ALERT" else 2),
            arrows={"to": {"enabled": True, "scaleFactor": 1.2}},
            label=("Selected · Flagged transfer" if payment["role"] == "ALERT" else "Selected")
            if selected else ("Flagged transfer" if payment["role"] == "ALERT" else ""),
            font={"size": 18, "color": "#172331", "strokeWidth": 4, "strokeColor": "#fafbfd"},
        )
    return network


@lru_cache(maxsize=1)
def _vis_assets() -> tuple[str, str]:
    directory = Path(pyvis.__file__).parent / "templates/lib/vis-9.1.2"
    return (directory.joinpath("vis-network.min.js").read_text(),
            directory.joinpath("vis-network.css").read_text())


def network_html(network: Network) -> str:
    """Embed bundled vis-network without CDN/Bootstrap dependencies or data-script injection."""
    payload = json.dumps({"nodes": network.nodes, "edges": network.edges}).replace(
        "<", "\\u003c"
    ).replace(">", "\\u003e").replace("&", "\\u0026")
    script, css = _vis_assets()
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<style>{css}</style>'
        '<style>html,body{margin:0;font-family:Arial}#network{height:420px;width:100%;'
        'border:1px solid #dce2e9;border-radius:8px;background:#fafbfd}.vis-button{filter:grayscale(1)}</style>'
        f'<script>{script}</script></head><body><div id="network"></div><script>'
        f'const graph={payload};'
        'const network=new vis.Network(document.getElementById("network"),'
        '{nodes:new vis.DataSet(graph.nodes),edges:new vis.DataSet(graph.edges)},'
        '{layout:{randomSeed:7},physics:false,'
        'interaction:{hover:true,keyboard:true,navigationButtons:true},edges:{smooth:{type:"dynamic"}},'
        'nodes:{font:{size:22}}});'
        'network.once("afterDrawing",()=>network.moveTo({position:{x:0,y:0},scale:Math.min(Math.max(network.getScale()*0.80,Math.min(0.6,document.getElementById("network").clientWidth/1050)),1)}));'
        '</script></body></html>'
    )


def render_agent_graph(member: dict, cited_ids=(), selected_id: str | None = None) -> None:
    st.subheader("Money-flow graph")
    graph = member["graph"]
    if graph["edges_capped"]:
        st.warning("Display edge cap: visualised payments are a subset of the observed records.")
    if graph["window_truncated"]:
        st.warning("The requested window extends beyond the dataset. Later payments are unavailable.")
    st.caption(
        f"Visible data ends: {graph['horizon_end']} UTC · "
        f"Hours observed after alert: {graph['hours_observed_after_alert']:g}"
    )
    if graph["no_graph"]:
        st.info("No surrounding payments are available in the saved money-flow graph.")
        if not graph_transactions(member):
            return
    st.caption(
        "Orange: alert sender · Red: alert receiver · Teal: upstream · Blue: downstream · "
        "Purple edges: finding citations · Thick brown edge labelled Selected: selected evidence"
    )
    if member["alert"]["txn_id"] not in {edge["txn_id"] for edge in graph_transactions(member)}:
        st.info("Alert payment details are unavailable; only the supplied surrounding payments can be drawn.")
    displayed_ids = {edge["txn_id"] for side in ("upstream", "downstream") for edge in graph[side]}
    st.caption(
        f"Displayed context transactions: {len(displayed_ids)} · "
        f"Observed upstream: {graph['n_upstream']} · Observed downstream: {graph['n_downstream']}"
    )
    st.caption("Arrows show payment direction. Drag to pan; use the graph controls to zoom or fit all accounts.")
    st.iframe(network_html(build_network(member, cited_ids, selected_id)), height=440)
