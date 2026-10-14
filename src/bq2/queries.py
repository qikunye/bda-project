"""Pure SQL builders for BQ2 alert lookup and case expansion."""

import re

_TRANSACTION_ID_PATTERN = re.compile(r"txn_[A-Za-z0-9]+")
_TABLE_NAME_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*"
)


def _validate_transaction_id(transaction_id: str) -> None:
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID_PATTERN.fullmatch(
        transaction_id
    ):
        raise ValueError("Invalid transaction ID format.")


def _validate_table_name(table_name: str) -> None:
    if not isinstance(table_name, str) or not _TABLE_NAME_PATTERN.fullmatch(table_name):
        raise ValueError("Invalid qualified Athena table name.")


def _validate_sql_literal(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value or "'" in value:
        raise ValueError(f"Invalid {field_name}.")


def build_alert_lookup_query(
    transaction_id: str,
    *,
    table_name: str,
) -> str:
    """Build the existing transaction-ID lookup query."""

    _validate_transaction_id(transaction_id)
    _validate_table_name(table_name)

    return f"""
    SELECT
        transaction_id,

        source_account_key,
        target_account_key,

        source_entity_id,
        source_entity_name,

        target_entity_id,
        target_entity_name,

        transaction_timestamp,

        amount_paid,
        payment_currency,
        payment_format

    FROM {table_name}

    WHERE transaction_id = '{transaction_id}'

    LIMIT 1
    """


def build_case_edges_query(
    alert_sender: str,
    alert_receiver: str,
    alert_timestamp: str,
    alert_transaction_id: str,
    window_minutes: int,
    *,
    table_name: str,
) -> str:
    """Build the existing alert-anchored upstream/downstream case query."""

    _validate_transaction_id(alert_transaction_id)
    _validate_sql_literal(alert_sender, field_name="alert sender")
    _validate_sql_literal(alert_receiver, field_name="alert receiver")
    _validate_sql_literal(alert_timestamp, field_name="alert timestamp")
    _validate_table_name(table_name)

    if (
        not isinstance(window_minutes, int)
        or isinstance(window_minutes, bool)
        or window_minutes <= 0
    ):
        raise ValueError("Window minutes must be a positive integer.")

    return f"""
    WITH parameters AS (

        SELECT
            '{alert_transaction_id}'
                AS alert_transaction_id,

            '{alert_sender}'
                AS alert_sender,

            '{alert_receiver}'
                AS alert_receiver,

            TIMESTAMP '{alert_timestamp}'
                AS alert_timestamp,

            {window_minutes}
                AS window_minutes
    ),

    alert_edge AS (

        SELECT
            'ALERT' AS edge_role,

            g.source_account_key,
            g.target_account_key,

            g.source_entity_id,
            g.source_entity_name,

            g.target_entity_id,
            g.target_entity_name,

            g.transaction_id,
            g.transaction_timestamp,

            0 AS gap_minutes,

            g.amount_paid,
            g.payment_currency,
            g.payment_format

        FROM {table_name} g

        CROSS JOIN parameters p

        WHERE
            g.transaction_id =
            p.alert_transaction_id

            AND
            g.txn_date =
            CAST(
                p.alert_timestamp
                AS DATE
            )
    ),

    upstream_edges AS (

        SELECT
            'UPSTREAM' AS edge_role,

            g.source_account_key,
            g.target_account_key,

            g.source_entity_id,
            g.source_entity_name,

            g.target_entity_id,
            g.target_entity_name,

            g.transaction_id,
            g.transaction_timestamp,

            DATE_DIFF(
                'minute',
                g.transaction_timestamp,
                p.alert_timestamp
            ) AS gap_minutes,

            g.amount_paid,
            g.payment_currency,
            g.payment_format

        FROM {table_name} g

        CROSS JOIN parameters p

        WHERE
            g.target_account_key =
            p.alert_sender

            AND g.txn_date BETWEEN

                CAST(
                    DATE_ADD(
                        'minute',
                        -p.window_minutes,
                        p.alert_timestamp
                    )
                    AS DATE
                )

                AND

                CAST(
                    p.alert_timestamp
                    AS DATE
                )

            AND
            g.transaction_timestamp
            <= p.alert_timestamp

            AND
            DATE_DIFF(
                'minute',
                g.transaction_timestamp,
                p.alert_timestamp
            )
            <= p.window_minutes
    ),

    downstream_edges AS (

        SELECT
            'DOWNSTREAM'
                AS edge_role,

            g.source_account_key,
            g.target_account_key,

            g.source_entity_id,
            g.source_entity_name,

            g.target_entity_id,
            g.target_entity_name,

            g.transaction_id,
            g.transaction_timestamp,

            DATE_DIFF(
                'minute',
                p.alert_timestamp,
                g.transaction_timestamp
            ) AS gap_minutes,

            g.amount_paid,
            g.payment_currency,
            g.payment_format

        FROM {table_name} g

        CROSS JOIN parameters p

        WHERE
            g.source_account_key =
            p.alert_receiver

            AND g.txn_date BETWEEN

                CAST(
                    p.alert_timestamp
                    AS DATE
                )

                AND

                CAST(
                    DATE_ADD(
                        'minute',
                        p.window_minutes,
                        p.alert_timestamp
                    )
                    AS DATE
                )

            AND
            g.transaction_timestamp
            >= p.alert_timestamp

            AND
            DATE_DIFF(
                'minute',
                p.alert_timestamp,
                g.transaction_timestamp
            )
            <= p.window_minutes
    )

    SELECT *
    FROM alert_edge

    UNION ALL

    SELECT *
    FROM upstream_edges

    UNION ALL

    SELECT *
    FROM downstream_edges
    """
