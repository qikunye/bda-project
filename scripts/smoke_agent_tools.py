"""Runs the agent's tools on three sample alerts against real Athena and checks the results,
the data scanned, caching, and that no label column appears. Needs an AWS login."""

import argparse
import json
import sys
import time

from src import config
from src.agent import tools
from src.agent.data import AgentData, athena_runner
from src.bq2.queries import build_case_edges_query

MAX_BYTES_PER_CALL = 150 * 1024 * 1024

CASES = [
    # (label, txn_id, txn_date, expectation)
    (
        "demo, 14 Sep, full window",
        "txn_a0f703e20da6f9bf0bdad4c0d62862de",
        "2022-09-14",
        "graph",
    ),
    (
        "16 Sep, truncated window",
        "txn_a8339a2e217fa5f40a54b2439e955bfc",
        "2022-09-16",
        "truncated",
    ),
    (
        "15 Sep, outside top 1%",
        "txn_4c04b1aa63ea3c438f55f273d5cad546",
        "2022-09-15",
        "outside the top 1%",
    ),
]


def timed(data: AgentData, call):
    start, before = time.perf_counter(), len(data.scan_log)
    result = call()
    calls = data.scan_log[before:]
    return result, {
        "seconds": round(time.perf_counter() - start, 2),
        "mb_scanned": round(sum(c.bytes_scanned for c in calls) / 1e6, 1),
        "max_call_mb": round(max((c.bytes_scanned for c in calls), default=0) / 1e6, 1),
        "queries": sum(not c.cached for c in calls),
        "cached": sum(c.cached for c in calls),
    }


def compare_with_bq2(data: AgentData, reference: AgentData, alert, window_h: int):
    """Check the agent's case query returns the same edges as the BQ2 owner's query."""

    graph = tools.get_case_graph(data, alert, window_h, max_edges=100_000)
    ours = {
        (role, e["txn_id"], e["gap_minutes"])
        for role, side in (("UPSTREAM", "upstream"), ("DOWNSTREAM", "downstream"))
        for e in graph[side]
    }
    frame = reference.query(
        build_case_edges_query(
            alert["sender"],
            alert["receiver"],
            alert["ts"],
            alert["txn_id"],
            window_h * 60,
            table_name="is459_aml_gold.v_agent_edges",
        ),
        "bq2 reference",
    )
    theirs = {
        (r["edge_role"], r["transaction_id"], int(r["gap_minutes"]))
        for r in frame.to_dict("records")
        if r["edge_role"] != "ALERT"
    }
    return ours == theirs, len(ours), len(theirs)


def run_case(
    data: AgentData,
    txn_id: str,
    txn_date: str,
    show_json: bool,
    reference: AgentData | None = None,
) -> list[str]:
    problems, outputs, rows = [], [], []

    alert, stats = timed(data, lambda: tools.get_alert(data, txn_id, txn_date))
    rows.append(("get_alert", stats))
    outputs.append(alert)
    if not alert.get("found"):
        return [f"{txn_id}: alert not found"]

    graph, stats = timed(data, lambda: tools.get_case_graph(data, alert, 48))
    rows.append(("get_case_graph 48h", stats))
    outputs.append(graph)

    shown = graph["upstream"] + graph["downstream"]
    neighbours, stats = timed(
        data,
        lambda: tools.get_neighbour_scores(
            data, [e["txn_id"] for e in shown], [e["txn_date"] for e in shown]
        ),
    )
    rows.append((f"get_neighbour_scores ({len(shown)} ids)", stats))
    outputs.append(neighbours)

    related, stats = timed(
        data,
        lambda: tools.get_related_alerts(
            data,
            [alert["sender"], alert["receiver"]],
            alert["ts"],
            48,
            exclude_txn_ids=(txn_id,),
        ),
    )
    rows.append(("get_related_alerts 48h", stats))
    outputs.append(related)

    for side in ("sender", "receiver"):
        activity, stats = timed(
            data, lambda side=side: tools.get_account_activity(data, alert, side, 48)
        )
        rows.append((f"get_account_activity {side}", stats))
        outputs.append(activity)
        print(
            f"  activity {side}: in={activity['n_incoming']} "
            f"({activity['n_incoming_counterparties']} accts) out={activity['n_outgoing']} "
            f"({activity['n_outgoing_counterparties']} accts) history_days={activity['history_days']} "
            f"capped={activity['edges_capped']}"
        )

    if reference is not None:
        for window_h in (6, 48, 72):
            same, n_ours, n_bq2 = compare_with_bq2(data, reference, alert, window_h)
            print(f"  BQ2 check {window_h}h: agent={n_ours} bq2={n_bq2} same={same}")
            if not same:
                problems.append(f"{txn_id}: case graph differs from BQ2 at {window_h}h")

    _, stats = timed(data, lambda: tools.get_alert(data, txn_id, txn_date))
    rows.append(("get_alert (repeat)", stats))
    if stats["queries"] != 0:
        problems.append(f"{txn_id}: repeated get_alert was not served from the cache")

    for name, s in rows:
        print(
            f"  {name:<32} {s['seconds']:>6}s  {s['mb_scanned']:>7} MB  "
            f"(max/call {s['max_call_mb']} MB, {s['queries']} queries, {s['cached']} cached)"
        )
        if s["max_call_mb"] * 1e6 > MAX_BYTES_PER_CALL:
            problems.append(
                f"{txn_id}: {name} scanned {s['max_call_mb']} MB in one call"
            )

    scored = [n for n in neighbours["neighbours"] if n["scored"]]
    print(
        f"  alert: rank_day={alert['risk_rank_day']} rank_global={alert['risk_rank_global']} "
        f"amount={alert['amount']} {alert['currency']}"
    )
    print(
        f"  graph: up={graph['n_upstream']} ({graph['n_upstream_accounts']} accts) "
        f"down={graph['n_downstream']} ({graph['n_downstream_accounts']} accts) "
        f"truncated={graph['window_truncated']} capped={graph['edges_capped']} "
        f"totals={graph['per_currency_totals']}\n"
        f"  flow: hours_observed={graph['hours_observed_after_alert']} "
        f"returned_to_source={len(graph['returned_to_source'])} flow={graph['flow']}"
    )
    print(
        f"  neighbours: {len(scored)}/{len(neighbours['neighbours'])} scored, "
        f"{sum(n['in_day_top_k'] for n in scored)} in top {config.AGENT_TOP_K}; "
        f"best rank_day={min((n['risk_rank_day'] for n in scored if n['risk_rank_day']), default=None)}"
    )
    print(f"  related alerts: {len(related['alerts'])}")

    text = json.dumps(outputs)
    for column in config.AGENT_FORBIDDEN_COLUMNS:
        if f'"{column}"' in text:
            problems.append(f"{txn_id}: forbidden column {column} in tool output")
    if show_json:
        print(json.dumps(outputs, indent=2, ensure_ascii=False))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--show-json", action="store_true")
    parser.add_argument(
        "--compare-bq2",
        action="store_true",
        help="also check the case graph against the BQ2 query (about 0.5-1 GB per case)",
    )
    args = parser.parse_args()

    runner = athena_runner()
    data = AgentData(runner=runner)
    reference = AgentData(runner=runner) if args.compare_bq2 else None
    problems = []
    for label, txn_id, txn_date, expectation in CASES:
        print(f"\n== {label}: {txn_id} (expect {expectation})")
        problems += run_case(data, txn_id, txn_date, args.show_json, reference)

    print(f"\nTotal scanned: {data.bytes_scanned / 1e6:.1f} MB")
    if problems:
        print("FAILED:\n  " + "\n  ".join(problems))
        return 1
    print("OK: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
