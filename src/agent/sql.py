"""Builds the SQL for the agent's tools from fixed column lists and validated inputs. Each statement
is checked so it can never request a label column.
"""

import re
from datetime import date, datetime, timedelta

from src import config

_TRANSACTION_ID = re.compile(r"txn_[A-Za-z0-9]+")
_ACCOUNT_KEY = re.compile(r"[0-9]+\|[0-9A-Za-z]+")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

SCORE_COLUMNS = (
    "transaction_id",
    "source_account_key",
    "target_account_key",
    "transaction_timestamp",
    "risk_score",
    "risk_rank_global",
    "risk_rank_day",
)
ALERT_COLUMNS = (
    *SCORE_COLUMNS,
    "amount_paid",
    "payment_currency",
    "payment_format",
    *(
        f"{name}_{n}"
        for n in (1, 2, 3)
        for name in ("feature", "reason", "contribution")
    ),
)


class LabelLeakError(RuntimeError):
    """Raised when a statement or result mentions a ground-truth column."""


def assert_label_free(sql: str) -> str:
    """Raise if the statement names any forbidden column; return it unchanged otherwise."""

    tokens = {token.lower() for token in _IDENTIFIER.findall(sql)}
    leaked = tokens & config.AGENT_FORBIDDEN_COLUMNS
    if leaked:
        raise LabelLeakError(
            f"Statement references forbidden columns: {sorted(leaked)}"
        )
    return sql


def validate_transaction_id(value: str) -> str:
    if not isinstance(value, str) or not _TRANSACTION_ID.fullmatch(value):
        raise ValueError(f"Invalid transaction ID: {value!r}")
    return value


def validate_account_key(value: str) -> str:
    if not isinstance(value, str) or not _ACCOUNT_KEY.fullmatch(value):
        raise ValueError(f"Invalid account key: {value!r}")
    return value


def validate_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid date: {value!r}") from error


def validate_timestamp(value: str) -> str:
    """Return the timestamp as 'YYYY-MM-DD HH:MM:SS' (Athena may add milliseconds)."""

    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid timestamp: {value!r}") from error


def validate_window_hours(value: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not config.AGENT_WINDOW_H_MIN <= value <= config.AGENT_WINDOW_H_MAX
    ):
        raise ValueError(
            f"Window must be an integer from {config.AGENT_WINDOW_H_MIN} "
            f"to {config.AGENT_WINDOW_H_MAX} hours, got {value!r}"
        )
    return value


def _date_list(dates) -> str:
    return ", ".join(f"DATE '{validate_date(d)}'" for d in sorted(set(dates)))


def _id_list(txn_ids) -> str:
    return ", ".join(f"'{validate_transaction_id(t)}'" for t in sorted(set(txn_ids)))


def build_alert_query(txn_id: str, txn_date: str) -> str:
    """Score, reasons and payment details for one alert (one bucket of one partition)."""

    txn_id = validate_transaction_id(txn_id)
    txn_date = validate_date(txn_date)
    columns = ",\n  ".join(ALERT_COLUMNS)
    return assert_label_free(f"""
SELECT
  {columns}
FROM {config.AGENT_SCORES_TABLE}
WHERE txn_date = DATE '{txn_date}'
  AND transaction_id = '{txn_id}'
""")


def build_case_graph_query(
    sender: str, receiver: str, ts: str, window_hours: int, as_of: str | None = None
) -> str:
    """One-hop upstream/downstream edges around an alert.

    Same rule as the BQ2 case query (src/bq2/queries.py): upstream = payments into the
    sender with ts <= alert and gap <= window; downstream = payments out of the receiver
    with ts >= alert and gap <= window. It reads the agent edge table, bucketed by
    account, and returns only the columns the agent uses. The alert row is not returned;
    get_alert already has it. With ``as_of``, payments after that time are left out.
    """

    sender = validate_account_key(sender)
    receiver = validate_account_key(receiver)
    anchor = datetime.fromisoformat(validate_timestamp(ts))
    window = timedelta(hours=validate_window_hours(window_hours))
    minutes = window_hours * 60
    at = f"TIMESTAMP '{anchor:%Y-%m-%d %H:%M:%S}'"
    up_dates = f"DATE '{(anchor - window).date()}' AND DATE '{anchor.date()}'"
    down_end = anchor + window
    cutoff = ""
    if as_of is not None:
        as_of_time = datetime.fromisoformat(validate_timestamp(as_of))
        down_end = min(down_end, as_of_time)
        cutoff = f"\n  AND transaction_timestamp <= TIMESTAMP '{as_of_time:%Y-%m-%d %H:%M:%S}'"
    down_dates = f"DATE '{anchor.date()}' AND DATE '{down_end.date()}'"
    return assert_label_free(f"""
SELECT
  'UPSTREAM' AS edge_role,
  transaction_id,
  counterparty_key AS source_account_key,
  account_key AS target_account_key,
  transaction_timestamp,
  DATE_DIFF('minute', transaction_timestamp, {at}) AS gap_minutes,
  amount_paid,
  payment_currency,
  payment_format
FROM {config.AGENT_EDGES_TABLE}
WHERE account_key = '{sender}'
  AND direction = 'in'
  AND txn_date BETWEEN {up_dates}
  AND transaction_timestamp <= {at}
  AND DATE_DIFF('minute', transaction_timestamp, {at}) <= {minutes}
UNION ALL
SELECT
  'DOWNSTREAM' AS edge_role,
  transaction_id,
  account_key AS source_account_key,
  counterparty_key AS target_account_key,
  transaction_timestamp,
  DATE_DIFF('minute', {at}, transaction_timestamp) AS gap_minutes,
  amount_paid,
  payment_currency,
  payment_format
FROM {config.AGENT_EDGES_TABLE}
WHERE account_key = '{receiver}'
  AND direction = 'out'
  AND txn_date BETWEEN {down_dates}
  AND transaction_timestamp >= {at}
  AND DATE_DIFF('minute', {at}, transaction_timestamp) <= {minutes}{cutoff}
""")


def build_neighbour_scores_query(txn_ids, txn_dates) -> str:
    """Scores for a set of transactions; the date list keeps the scan to those partitions."""

    if not txn_ids or not txn_dates:
        raise ValueError("Need at least one transaction ID and one date.")
    return assert_label_free(f"""
SELECT
  transaction_id,
  risk_score,
  risk_rank_global,
  risk_rank_day,
  feature_1 IS NOT NULL AS has_reasons
FROM {config.AGENT_SCORES_TABLE}
WHERE txn_date IN ({_date_list(txn_dates)})
  AND transaction_id IN ({_id_list(txn_ids)})
""")


def build_day_queue_query(txn_date: str, top_k: int) -> str:
    """The top-K alerts of one day by per-day rank (test rows only)."""

    txn_date = validate_date(txn_date)
    if (
        not isinstance(top_k, int)
        or isinstance(top_k, bool)
        or not 0 < top_k <= config.AGENT_QUEUE_MAX_K
    ):
        raise ValueError(
            f"top_k must be an integer from 1 to {config.AGENT_QUEUE_MAX_K}, got {top_k!r}"
        )
    columns = ",\n  ".join(SCORE_COLUMNS)
    return assert_label_free(f"""
SELECT
  {columns}
FROM {config.AGENT_QUEUE_TABLE}
WHERE txn_date = DATE '{txn_date}'
  AND risk_rank_day <= {top_k}
ORDER BY risk_rank_day
""")
