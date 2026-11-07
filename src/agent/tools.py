"""The agent's read-only tools (PLAN.md 5.2, 18).

Plain functions over an ``AgentData`` source. They return compact JSON-ready dicts, never the
label, and list every transaction/account ID they show in ``ids``. Flags such as
``window_truncated`` and ``no_graph`` are computed here, so the LLM cannot assert them falsely.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from src import config
from src.agent import sql
from src.agent.contracts import (
    Alert,
    CaseGraph,
    Edge,
    NeighbourScore,
    NeighbourScores,
    Reason,
    RelatedAlert,
    RelatedAlerts,
)
from src.agent.data import AgentData


def _present(value) -> bool:
    return (
        value is not None
        and not (isinstance(value, float) and pd.isna(value))
        and value != ""
    )


def _int(value) -> int | None:
    return int(value) if _present(value) else None


def _float(value, digits: int) -> float | None:
    return round(float(value), digits) if _present(value) else None


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(sql.validate_timestamp(value))


def _ts_text(value: str) -> str:
    return sql.validate_timestamp(value)


def _unique(values) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _parse_reasons(row) -> list[Reason] | None:
    reasons: list[Reason] = []
    for n in (1, 2, 3):
        feature = row.get(f"feature_{n}")
        if not _present(feature):
            continue
        contribution = float(row[f"contribution_{n}"])
        sentence = str(row[f"reason_{n}"]).lstrip("↑↓ ").strip()
        reasons.append(
            Reason(
                feature=str(feature),
                sentence=sentence,
                direction="up" if contribution >= 0 else "down",
                contribution=round(contribution, 3),
            )
        )
    return reasons or None


def get_alert(
    data: AgentData, txn_id: str, txn_date: str, *, top_k: int = config.AGENT_TOP_K
) -> Alert:
    """Score, per-day rank, reasons (or null) and payment details of one alert."""

    txn_id = sql.validate_transaction_id(txn_id)
    txn_date = sql.validate_date(txn_date)
    if txn_date not in config.AGENT_SCORED_DATES:
        return Alert(found=False, txn_id=txn_id, txn_date=txn_date, ids=[])

    frame = data.query(sql.build_alert_query(txn_id, txn_date), f"get_alert {txn_id}")
    if frame.empty:
        return Alert(found=False, txn_id=txn_id, txn_date=txn_date, ids=[])

    row = frame.iloc[0].to_dict()
    rank_day = _int(row["risk_rank_day"])
    sender, receiver = row["source_account_key"], row["target_account_key"]
    return Alert(
        found=True,
        txn_id=txn_id,
        txn_date=txn_date,
        ts=_ts_text(row["transaction_timestamp"]),
        sender=sender,
        receiver=receiver,
        amount=_float(row["amount_paid"], 2),
        currency=row["payment_currency"] if _present(row["payment_currency"]) else None,
        payment_format=row["payment_format"]
        if _present(row["payment_format"])
        else None,
        risk_score=_float(row["risk_score"], 4),
        risk_rank_global=_int(row["risk_rank_global"]),
        risk_rank_day=rank_day,
        in_queue=rank_day is not None,
        in_day_top_k=rank_day is not None and rank_day <= top_k,
        reasons=_parse_reasons(row),
        ids=_unique([txn_id, sender, receiver]),
    )


def _horizon(as_of: str | None) -> datetime:
    """The last moment the agent may see: the end of the data, or an earlier cutoff."""

    horizon = datetime.fromisoformat(config.AGENT_DATA_HORIZON)
    return min(horizon, _ts(as_of)) if as_of else horizon


def _edge(row) -> Edge:
    ts = _ts_text(row["transaction_timestamp"])
    return Edge(
        txn_id=row["transaction_id"],
        txn_date=ts[:10],
        ts=ts,
        sender=row["source_account_key"],
        receiver=row["target_account_key"],
        amount=_float(row["amount_paid"], 2),
        currency=row["payment_currency"],
        payment_format=row["payment_format"],
        gap_minutes=int(row["gap_minutes"]),
    )


def _currency_totals(rows: pd.DataFrame) -> dict[str, float]:
    totals: dict[str, Decimal] = {}
    for amount, currency in zip(rows["amount_paid"], rows["payment_currency"]):
        totals[currency] = totals.get(currency, Decimal(0)) + Decimal(str(amount))
    return {
        currency: round(float(total), 2) for currency, total in sorted(totals.items())
    }


def get_case_graph(
    data: AgentData,
    alert: Alert,
    window_h: int = config.AGENT_WINDOW_H_DEFAULT,
    *,
    max_edges: int = config.AGENT_MAX_EDGES_PER_DIRECTION,
    as_of: str | None = None,
) -> CaseGraph:
    """Upstream payments into the sender and downstream payments out of the receiver (BQ2).

    With ``as_of``, payments after that time are hidden and the window counts as truncated there.
    """

    if not alert.get("found"):
        raise ValueError(
            "get_case_graph needs an alert returned by get_alert with found=True."
        )
    window_h = sql.validate_window_hours(window_h)

    frame = data.query(
        sql.build_case_graph_query(
            alert["sender"], alert["receiver"], alert["ts"], window_h, as_of=as_of
        ),
        f"get_case_graph {alert['txn_id']} {window_h}h",
    )
    sides: dict[str, pd.DataFrame] = {}
    for role in ("UPSTREAM", "DOWNSTREAM"):
        rows = frame[frame["edge_role"] == role].copy()
        rows["gap_minutes"] = rows["gap_minutes"].astype(int)
        sides[role] = rows.sort_values(["gap_minutes", "transaction_id"])

    upstream = [_edge(r) for r in sides["UPSTREAM"].head(max_edges).to_dict("records")]
    downstream = [
        _edge(r) for r in sides["DOWNSTREAM"].head(max_edges).to_dict("records")
    ]
    n_upstream, n_downstream = len(sides["UPSTREAM"]), len(sides["DOWNSTREAM"])

    horizon = _horizon(as_of)
    shown = upstream + downstream
    return CaseGraph(
        txn_id=alert["txn_id"],
        window_h=window_h,
        upstream=upstream,
        downstream=downstream,
        n_upstream=n_upstream,
        n_downstream=n_downstream,
        n_upstream_accounts=sides["UPSTREAM"]["source_account_key"].nunique(),
        n_downstream_accounts=sides["DOWNSTREAM"]["target_account_key"].nunique(),
        per_currency_totals={
            "upstream": _currency_totals(sides["UPSTREAM"]),
            "downstream": _currency_totals(sides["DOWNSTREAM"]),
        },
        no_graph=n_upstream == 0 and n_downstream == 0,
        edges_capped=n_upstream > len(upstream) or n_downstream > len(downstream),
        window_truncated=_ts(alert["ts"]) + timedelta(hours=window_h) > horizon,
        horizon_end=str(horizon),
        ids=_unique(
            [alert["txn_id"], alert["sender"], alert["receiver"]]
            + [e["txn_id"] for e in shown]
            + [e["sender"] for e in shown]
            + [e["receiver"] for e in shown]
        ),
    )


def get_neighbour_scores(
    data: AgentData,
    txn_ids: list[str],
    txn_dates: list[str],
    *,
    top_k: int = config.AGENT_TOP_K,
) -> NeighbourScores:
    """BQ1 scores of neighbour transactions. Dates outside the scored days come back unscored."""

    if len(txn_ids) != len(txn_dates):
        raise ValueError("txn_ids and txn_dates must have the same length.")
    if len(set(txn_ids)) > config.AGENT_MAX_NEIGHBOUR_IDS:
        raise ValueError(
            f"At most {config.AGENT_MAX_NEIGHBOUR_IDS} neighbour IDs per call."
        )

    pairs = list(
        dict.fromkeys(
            (sql.validate_transaction_id(t), sql.validate_date(d))
            for t, d in zip(txn_ids, txn_dates)
        )
    )
    lookup = [(t, d) for t, d in pairs if d in config.AGENT_SCORED_DATES]
    found: dict[str, dict] = {}
    if lookup:
        frame = data.query(
            sql.build_neighbour_scores_query(
                [t for t, _ in lookup], [d for _, d in lookup]
            ),
            f"get_neighbour_scores {len(lookup)} ids",
        )
        found = {row["transaction_id"]: row for row in frame.to_dict("records")}

    neighbours: list[NeighbourScore] = []
    for txn_id, txn_date in pairs:
        row = found.get(txn_id)
        rank_day = _int(row["risk_rank_day"]) if row else None
        neighbours.append(
            NeighbourScore(
                txn_id=txn_id,
                txn_date=txn_date,
                scored=row is not None,
                risk_score=_float(row["risk_score"], 4) if row else None,
                risk_rank_global=_int(row["risk_rank_global"]) if row else None,
                risk_rank_day=rank_day,
                in_queue=rank_day is not None,
                in_day_top_k=rank_day is not None and rank_day <= top_k,
                has_reasons=bool(row) and str(row["has_reasons"]).lower() == "true",
            )
        )
    return NeighbourScores(neighbours=neighbours, ids=[t for t, _ in pairs])


def load_day_queue(
    data: AgentData, txn_date: str, top_k: int = config.AGENT_TOP_K
) -> pd.DataFrame:
    """The top-K alerts of one scored day. Cached by ``data``, so it costs one query per day."""

    return data.query(
        sql.build_day_queue_query(txn_date, top_k), f"day_queue {txn_date} top{top_k}"
    )


def get_related_alerts(
    data: AgentData,
    accounts: list[str],
    anchor_ts: str,
    window_h: int = config.AGENT_WINDOW_H_DEFAULT,
    *,
    exclude_txn_ids: tuple[str, ...] = (),
    top_k: int = config.AGENT_TOP_K,
    as_of: str | None = None,
) -> RelatedAlerts:
    """Other top-K alerts that touch the same accounts within +/- window of the anchor.

    With ``as_of``, alerts after that time are left out.
    """

    accounts = [sql.validate_account_key(a) for a in dict.fromkeys(accounts)]
    window_h = sql.validate_window_hours(window_h)
    anchor = _ts(anchor_ts)
    window = timedelta(hours=window_h)
    excluded = set(exclude_txn_ids)

    horizon = _horizon(as_of)
    first, last = (anchor - window).date(), min(anchor + window, horizon).date()
    days = [
        d for d in config.AGENT_SCORED_DATES if first <= date.fromisoformat(d) <= last
    ]

    alerts: list[RelatedAlert] = []
    for day in days:
        for row in load_day_queue(data, day, top_k).to_dict("records"):
            if row["transaction_id"] in excluded:
                continue
            ts = _ts(row["transaction_timestamp"])
            if abs(ts - anchor) > window or ts > horizon:
                continue
            for account in accounts:
                for role, key in (
                    ("sender", "source_account_key"),
                    ("receiver", "target_account_key"),
                ):
                    if row[key] == account:
                        alerts.append(
                            RelatedAlert(
                                txn_id=row["transaction_id"],
                                txn_date=day,
                                ts=_ts_text(row["transaction_timestamp"]),
                                risk_rank_day=int(row["risk_rank_day"]),
                                account=account,
                                role=role,
                            )
                        )

    alerts.sort(key=lambda a: (a["risk_rank_day"], a["txn_id"], a["account"]))
    return RelatedAlerts(
        alerts=alerts,
        top_k=top_k,
        window_h=window_h,
        ids=_unique([a["txn_id"] for a in alerts] + [a["account"] for a in alerts]),
    )
