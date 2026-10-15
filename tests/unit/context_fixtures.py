"""Synthetic-only fixtures shared by CaseContext unit tests."""

from datetime import datetime, timezone

import pandas as pd

from src.bq1.contracts import BQ1MetadataResult, BQ1RiskMetadata
from src.config import BQ1_SCORE_TABLE
from src.context.builder import build_case_context
from src.shared.contracts import AlertHandoff

ALERT_ID = "txn_synthetic_alert"
ALERT_TIMESTAMP = datetime(2022, 9, 3, 12, 0, tzinfo=timezone.utc)
SENDER = "SYNTHETIC_ACCOUNT_A"
RECEIVER = "SYNTHETIC_ACCOUNT_B"

RAW_COLUMNS = [
    "edge_role",
    "transaction_id",
    "transaction_timestamp",
    "source_account_key",
    "target_account_key",
    "source_entity_id",
    "source_entity_name",
    "target_entity_id",
    "target_entity_name",
    "amount_paid",
    "payment_currency",
    "payment_format",
    "gap_minutes",
]


def raw_edge(
    role: str,
    transaction_id: str,
    timestamp: str,
    source: str,
    target: str,
    amount: object,
    currency: str | None,
    payment_format: str,
    gap_minutes: int,
) -> dict[str, object]:
    return {
        "edge_role": role,
        "transaction_id": transaction_id,
        "transaction_timestamp": timestamp,
        "source_account_key": source,
        "target_account_key": target,
        "source_entity_id": f"entity-{source}",
        "source_entity_name": f"Synthetic {source}",
        "target_entity_id": f"entity-{target}",
        "target_entity_name": f"Synthetic {target}",
        "amount_paid": amount,
        "payment_currency": currency,
        "payment_format": payment_format,
        "gap_minutes": gap_minutes,
    }


def make_raw_edges() -> pd.DataFrame:
    rows = [
        raw_edge(
            "ALERT",
            ALERT_ID,
            "2022-09-03 12:00:00",
            SENDER,
            RECEIVER,
            "100.25",
            "USD",
            "WIRE",
            0,
        ),
        raw_edge(
            "UPSTREAM",
            "txn_synthetic_u1",
            "2022-09-03 09:00:00",
            "SYNTHETIC_UPSTREAM_1",
            SENDER,
            "40.00",
            "USD",
            "ACH",
            180,
        ),
        raw_edge(
            "UPSTREAM",
            "txn_synthetic_u2",
            "2022-09-02 12:00:00",
            "SYNTHETIC_UPSTREAM_1",
            SENDER,
            "60.00",
            "SGD",
            "CARD",
            1440,
        ),
        raw_edge(
            "UPSTREAM",
            "txn_synthetic_u3",
            "2022-09-03 11:00:00",
            "SYNTHETIC_UPSTREAM_2",
            SENDER,
            None,
            "USD",
            "WIRE",
            60,
        ),
        raw_edge(
            "DOWNSTREAM",
            "txn_synthetic_d1",
            "2022-09-03 13:00:00",
            RECEIVER,
            "SYNTHETIC_DOWNSTREAM_1",
            "25.50",
            "EUR",
            "ACH",
            60,
        ),
        raw_edge(
            "DOWNSTREAM",
            "txn_synthetic_d2",
            "2022-09-03 14:00:00",
            RECEIVER,
            "SYNTHETIC_DOWNSTREAM_1",
            "not-an-amount",
            "EUR",
            "ACH",
            120,
        ),
        raw_edge(
            "DOWNSTREAM",
            "txn_synthetic_d3",
            "2022-09-03 15:00:00",
            RECEIVER,
            "SYNTHETIC_DOWNSTREAM_2",
            "0",
            "USD",
            "CARD",
            180,
        ),
        raw_edge(
            "DOWNSTREAM",
            "txn_synthetic_d4",
            "2022-09-03 16:00:00",
            RECEIVER,
            "SYNTHETIC_UPSTREAM_1",
            "15.75",
            "USD",
            "WIRE",
            240,
        ),
    ]
    return pd.DataFrame(rows, columns=RAW_COLUMNS)


def make_handoff(**overrides: object) -> AlertHandoff:
    values = {
        "transaction_id": ALERT_ID,
        "source_account_key": SENDER,
        "target_account_key": RECEIVER,
        "transaction_timestamp": ALERT_TIMESTAMP,
    }
    values.update(overrides)
    return AlertHandoff(**values)


def make_bq1_result(**overrides: object) -> BQ1MetadataResult:
    values = {
        "transaction_id": ALERT_ID,
        "source_account_key": SENDER,
        "target_account_key": RECEIVER,
        "transaction_timestamp": ALERT_TIMESTAMP,
        "risk_score": 0.84,
        "risk_rank": 12,
        "model_name": "synthetic_xgboost",
        "model_version": "synthetic-v1",
        "source_table": BQ1_SCORE_TABLE,
    }
    values.update(overrides)
    return BQ1MetadataResult.available(BQ1RiskMetadata(**values))


def make_context() -> dict[str, object]:
    return build_case_context(
        handoff=make_handoff(),
        raw_edges=make_raw_edges(),
        bq1_result=make_bq1_result(),
        queried_window_hours=48,
    )
