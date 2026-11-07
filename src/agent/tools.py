"""The read-only tools the agent uses to look up alerts, payments, account activity, neighbour
scores and related alerts. They never return labels, and they list every ID they show."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from src import config
from src.agent import sql
from src.agent.contracts import (
    AccountActivity,
    ActivityPayment,
    Alert,
    CaseGraph,
    CurrencyFlow,
    DailyActivity,
    Edge,
    NeighbourScore,
    NeighbourScores,
    RelatedAlert,
    RelatedAlerts,
    ReturnedToSource,
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


def get_alert(
    data: AgentData, txn_id: str, txn_date: str, *, top_k: int = config.AGENT_TOP_K
) -> Alert:
    """Score, per-day rank and payment details of one alert. The risk model's reasons are never returned."""

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
        ids=_unique([txn_id, sender, receiver]),
    )


def _horizon(as_of: str | None) -> datetime:
    """The last moment the agent may see: the end of the data, or an earlier cutoff."""

    horizon = datetime.fromisoformat(config.AGENT_DATA_HORIZON)
    return min(horizon, _ts(as_of)) if as_of else horizon


def _past_data_end(alert_ts: str, window_h: int) -> bool:
    """Whether the window runs past the end of the dataset. A daily run's own end-of-day cutoff does
    not count: it cuts every 48 h window, so it says nothing (hours_observed_after_alert covers it)."""

    return _ts(alert_ts) + timedelta(hours=window_h) > datetime.fromisoformat(
        config.AGENT_DATA_HORIZON
    )


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


def _hours_observed(alert_ts: str, window_h: int, horizon: datetime) -> float:
    """Hours from the alert to the end of visible data, never more than the window or below zero."""

    hours = (horizon - _ts(alert_ts)).total_seconds() / 3600
    return round(max(0.0, min(float(window_h), hours)), 1)


def _returned_to_source(
    alert: Alert, upstream: pd.DataFrame, downstream: pd.DataFrame, limit: int
) -> list[ReturnedToSource]:
    """Downstream payments that go to the alert's sender or to the sender of an upstream payment.

    Both frames are sorted by gap, so the first upstream payment per sender is the closest to the
    alert, and entries come out earliest first. All edges are checked, not only the shown ones.
    """

    closest: dict[str, str] = {}
    for row in upstream.to_dict("records"):
        closest.setdefault(row["source_account_key"], row["transaction_id"])

    returned: list[ReturnedToSource] = []
    for row in downstream.to_dict("records"):
        account = row["target_account_key"]
        if account == alert["sender"]:
            kind, source = "alert_sender", alert["txn_id"]
        elif account in closest:
            kind, source = "upstream_source", closest[account]
        else:
            continue
        returned.append(
            ReturnedToSource(
                account=account,
                kind=kind,
                downstream_txn_id=row["transaction_id"],
                source_txn_id=source,
                gap_minutes=int(row["gap_minutes"]),
            )
        )
    return returned[:limit]


def _flow(
    alert: Alert, upstream: pd.DataFrame, downstream: pd.DataFrame
) -> dict[str, CurrencyFlow]:
    """Per currency: the money in and out, and how fast it went on. All edges, not only shown.

    ``quick_onward_share`` compares the receiver's onward payments with the alert amount, because the alert
    is the payment that links the two accounts. It exists only in the alert's own currency, and counts
    only payments within AGENT_PASS_THROUGH_MAX_GAP_MIN of the alert, so a busy receiver's later
    payments over the rest of the window do not count.
    """

    up_totals, down_totals = _currency_totals(upstream), _currency_totals(downstream)
    alert_amount = alert.get("amount") or 0.0
    flow: dict[str, CurrencyFlow] = {}
    for currency in sorted(set(up_totals) | set(down_totals)):
        onward = downstream[downstream["payment_currency"] == currency]
        quick = onward[onward["gap_minutes"] <= config.AGENT_PASS_THROUGH_MAX_GAP_MIN]
        quick_total = _currency_totals(quick).get(currency, 0.0)
        total_in, total_out = up_totals.get(currency, 0.0), down_totals.get(currency, 0.0)
        flow[currency] = CurrencyFlow(
            upstream_total=total_in,
            downstream_total=total_out,
            quick_onward_share=round(quick_total / alert_amount, 3)
            if alert_amount > 0 and currency == alert.get("currency")
            else None,
            fastest_onward_gap_minutes=int(onward["gap_minutes"].iloc[0])
            if len(onward)
            else None,
            median_onward_gap_minutes=round(float(onward["gap_minutes"].median()), 1)
            if len(onward)
            else None,
            fastest_onward_txn_id=onward["transaction_id"].iloc[0]
            if len(onward)
            else None,
        )
    return flow


def get_case_graph(
    data: AgentData,
    alert: Alert,
    window_h: int = config.AGENT_WINDOW_H_DEFAULT,
    *,
    max_edges: int = config.AGENT_MAX_EDGES_PER_DIRECTION,
    as_of: str | None = None,
) -> CaseGraph:
    """Upstream payments into the sender and downstream payments out of the receiver (BQ2).

    With ``as_of``, payments after that time are hidden; hours_observed_after_alert shows how much
    is left. window_truncated is set only when the window runs past the end of the dataset.
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
    return case_graph_from_frame(
        alert, frame, window_h, max_edges=max_edges, as_of=as_of
    )


def case_graph_from_frame(
    alert: Alert,
    frame: pd.DataFrame,
    window_h: int,
    *,
    max_edges: int = config.AGENT_MAX_EDGES_PER_DIRECTION,
    as_of: str | None = None,
) -> CaseGraph:
    """Turns the rows of ``build_case_graph_query`` into a CaseGraph. Pure, so samples can use it."""

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
    returned = _returned_to_source(
        alert,
        sides["UPSTREAM"],
        sides["DOWNSTREAM"],
        config.AGENT_MAX_RETURNED_TO_SOURCE,
    )
    flow = _flow(alert, sides["UPSTREAM"], sides["DOWNSTREAM"])
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
        window_truncated=_past_data_end(alert["ts"], window_h),
        horizon_end=str(horizon),
        hours_observed_after_alert=_hours_observed(alert["ts"], window_h, horizon),
        returned_to_source=returned,
        flow=flow,
        ids=_unique(
            [alert["txn_id"], alert["sender"], alert["receiver"]]
            + [e["txn_id"] for e in shown]
            + [e["sender"] for e in shown]
            + [e["receiver"] for e in shown]
            + [r["downstream_txn_id"] for r in returned]
            + [r["source_txn_id"] for r in returned]
            + [r["account"] for r in returned]
            + [f["fastest_onward_txn_id"] for f in flow.values()]
        ),
    )


def _activity_payment(row) -> ActivityPayment:
    ts = _ts_text(row["transaction_timestamp"])
    return ActivityPayment(
        txn_id=row["transaction_id"],
        txn_date=ts[:10],
        ts=ts,
        counterparty=row["counterparty_key"],
        amount=_float(row["amount_paid"], 2),
        currency=row["payment_currency"],
        payment_format=row["payment_format"],
        offset_minutes=int(row["offset_minutes"]),
    )


def _daily_history(frame: pd.DataFrame) -> list[DailyActivity]:
    days: dict[str, DailyActivity] = {}
    for row in frame.to_dict("records"):
        day = str(row["txn_date"])[:10]
        entry = days.setdefault(
            day,
            DailyActivity(
                date=day,
                n_incoming=0,
                n_outgoing=0,
                incoming_totals={},
                outgoing_totals={},
            ),
        )
        side = "incoming" if row["direction"] == "in" else "outgoing"
        entry[f"n_{side}"] += int(row["n_payments"])
        totals = entry[f"{side}_totals"]
        totals[row["payment_currency"]] = round(
            float(
                Decimal(str(totals.get(row["payment_currency"], 0)))
                + Decimal(str(row["total_amount"]))
            ),
            2,
        )
    return [days[day] for day in sorted(days)]


def get_account_activity(
    data: AgentData,
    alert: Alert,
    side: str,
    window_h: int = config.AGENT_WINDOW_H_DEFAULT,
    *,
    max_edges: int = config.AGENT_MAX_EDGES_PER_DIRECTION,
    as_of: str | None = None,
) -> AccountActivity:
    """Other payments of the alert's sender or receiver, in and out, within +/- window of the alert.

    The alert itself is left out. Also gives daily counts and totals for the whole days before the
    alert's day, back to the first day the edge table covers. Those days can overlap the start of the
    window. With ``as_of``, payments after that time are hidden; window_truncated is set only when the
    window runs past the end of the dataset.
    """

    if not alert.get("found"):
        raise ValueError(
            "get_account_activity needs an alert returned by get_alert with found=True."
        )
    if side not in ("sender", "receiver"):
        raise ValueError(f"side must be 'sender' or 'receiver', got {side!r}")
    window_h = sql.validate_window_hours(window_h)
    account = alert[side]

    frame = data.query(
        sql.build_account_activity_query(
            account, alert["txn_id"], alert["ts"], window_h, as_of=as_of
        ),
        f"get_account_activity {alert['txn_id']} {side} {window_h}h",
    )
    history_days = _history_days(alert)
    history = (
        data.query(
            sql.build_account_history_query(account, alert["ts"]),
            f"get_account_activity history {alert['txn_id']} {side}",
        )
        if history_days
        else None
    )
    return account_activity_from_frames(
        alert, side, frame, history, window_h, max_edges=max_edges, as_of=as_of
    )


def _history_days(alert: Alert) -> int:
    return max(
        0,
        (
            _ts(alert["ts"]).date() - date.fromisoformat(config.AGENT_EDGES_FIRST_DAY)
        ).days,
    )


def account_activity_from_frames(
    alert: Alert,
    side: str,
    frame: pd.DataFrame,
    history: pd.DataFrame | None,
    window_h: int,
    *,
    max_edges: int = config.AGENT_MAX_EDGES_PER_DIRECTION,
    as_of: str | None = None,
) -> AccountActivity:
    """Turns the rows of the two activity queries into an AccountActivity. Pure, so samples can use it.

    ``history`` is None when the alert's day is the first day covered, so there is nothing to query.
    """

    account = alert[side]
    sides: dict[str, pd.DataFrame] = {}
    for direction in ("in", "out"):
        rows = frame[frame["direction"] == direction].copy()
        rows["offset_minutes"] = rows["offset_minutes"].astype(int)
        rows["distance"] = rows["offset_minutes"].abs()
        sides[direction] = rows.sort_values(["distance", "transaction_id"])

    incoming = [
        _activity_payment(r) for r in sides["in"].head(max_edges).to_dict("records")
    ]
    outgoing = [
        _activity_payment(r) for r in sides["out"].head(max_edges).to_dict("records")
    ]
    n_in, n_out = len(sides["in"]), len(sides["out"])

    history_days = _history_days(alert)
    daily_history = _daily_history(history) if history is not None else []

    horizon = _horizon(as_of)
    shown = incoming + outgoing
    return AccountActivity(
        txn_id=alert["txn_id"],
        side=side,
        account=account,
        window_h=window_h,
        incoming=incoming,
        outgoing=outgoing,
        n_incoming=n_in,
        n_outgoing=n_out,
        n_incoming_counterparties=sides["in"]["counterparty_key"].nunique(),
        n_outgoing_counterparties=sides["out"]["counterparty_key"].nunique(),
        per_currency_totals={
            "incoming": _currency_totals(sides["in"]),
            "outgoing": _currency_totals(sides["out"]),
        },
        no_activity=n_in == 0 and n_out == 0,
        edges_capped=n_in > len(incoming) or n_out > len(outgoing),
        window_truncated=_past_data_end(alert["ts"], window_h),
        hours_observed_after_alert=_hours_observed(alert["ts"], window_h, horizon),
        horizon_end=str(horizon),
        history_days=history_days,
        daily_history=daily_history,
        ids=_unique(
            [alert["txn_id"], account]
            + [p["txn_id"] for p in shown]
            + [p["counterparty"] for p in shown]
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
    """Other top-K alerts that touch the given accounts within +/- window of the anchor.

    The accounts are the alert's sender and receiver, and may also include counterparties from its
    graph. Each alert comes back once per account it touches. With ``as_of``, alerts after that
    time are left out.
    """

    accounts = [sql.validate_account_key(a) for a in dict.fromkeys(accounts)]
    if not accounts:
        raise ValueError("Need at least one account.")
    if len(accounts) > config.AGENT_MAX_RELATED_ACCOUNTS:
        raise ValueError(
            f"At most {config.AGENT_MAX_RELATED_ACCOUNTS} accounts per call, got {len(accounts)}."
        )
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
