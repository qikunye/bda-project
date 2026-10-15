"""Pure SQL builders for BQ1 risk-metadata lookup."""

import re
from datetime import datetime, timezone

_TRANSACTION_ID_PATTERN = re.compile(r"txn_[A-Za-z0-9]+")
_TABLE_NAME_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*"
)
_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
)


def _validate_transaction_id(transaction_id: str) -> None:
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID_PATTERN.fullmatch(
        transaction_id
    ):
        raise ValueError("Invalid transaction ID format.")


def _validate_table_name(table_name: str) -> None:
    if not isinstance(table_name, str) or not _TABLE_NAME_PATTERN.fullmatch(table_name):
        raise ValueError("Invalid qualified Athena table name.")


def _parse_transaction_timestamp(transaction_timestamp: str) -> datetime:
    if not isinstance(transaction_timestamp, str):
        raise TypeError("Invalid transaction timestamp.")

    value = transaction_timestamp.strip()

    for timestamp_format in _TIMESTAMP_FORMATS:
        try:
            parsed = datetime.strptime(value, timestamp_format).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue

        if parsed.microsecond == 0:
            return parsed

    raise ValueError("Invalid transaction timestamp.")


def build_bq1_score_lookup_query(
    transaction_id: str,
    transaction_timestamp: str,
    *,
    table_name: str,
) -> str:
    """Build a partition-pruned lookup for one selected BQ1 transaction."""

    _validate_transaction_id(transaction_id)
    _validate_table_name(table_name)
    parsed_timestamp = _parse_transaction_timestamp(transaction_timestamp)
    transaction_date = parsed_timestamp.strftime("%Y-%m-%d")

    # No LIMIT is used: the service must be able to detect duplicate rows.
    return f"""
    SELECT
        transaction_id,
        source_account_key,
        target_account_key,
        transaction_timestamp,
        risk_score,
        risk_rank,
        model_name,
        model_version

    FROM {table_name}

    WHERE transaction_id = '{transaction_id}'
      AND txn_date = DATE '{transaction_date}'
    """
