"""Offline demonstration of the public CaseContext integration service."""

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import BQ1_SCORE_TABLE, BQ2_EDGE_TABLE
from src.context.projection import build_llm_input
from src.context.service import load_case_context
from src.context.validation import (
    serialize_case_context,
    validate_case_context,
    validate_llm_input,
)
from src.shared.contracts import AlertHandoff


def synthetic_bq2_rows() -> pd.DataFrame:
    columns = [
        "edge_role",
        "transaction_id",
        "transaction_timestamp",
        "source_account_key",
        "target_account_key",
        "source_entity_id",
        "source_entity_name",
        "target_entity_id",
        "target_entity_name",
        "gap_minutes",
        "amount_paid",
        "payment_currency",
        "payment_format",
    ]
    rows = [
        [
            "ALERT",
            "txn_DemoAlert001",
            "2022-09-03 12:00:00",
            "SYNTHETIC_DEMO_A",
            "SYNTHETIC_DEMO_B",
            "entity-a",
            "Synthetic Sender",
            "entity-b",
            "Synthetic Receiver",
            "0",
            "100.25",
            "USD",
            "WIRE",
        ],
        [
            "UPSTREAM",
            "txn_DemoUpstream001",
            "2022-09-03 10:00:00",
            "SYNTHETIC_DEMO_UPSTREAM",
            "SYNTHETIC_DEMO_A",
            "entity-up",
            "Synthetic Upstream",
            "entity-a",
            "Synthetic Sender",
            "120",
            "35.00",
            "USD",
            "ACH",
        ],
        [
            "DOWNSTREAM",
            "txn_DemoDownstream001",
            "2022-09-03 14:00:00",
            "SYNTHETIC_DEMO_B",
            "SYNTHETIC_DEMO_DOWNSTREAM",
            "entity-b",
            "Synthetic Receiver",
            "entity-down",
            "Synthetic Downstream",
            "120",
            "20.00",
            "EUR",
            "CARD",
        ],
    ]
    return pd.DataFrame(rows, columns=columns)


def synthetic_bq1_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "transaction_id": "txn_DemoAlert001",
                "source_account_key": "SYNTHETIC_DEMO_A",
                "target_account_key": "SYNTHETIC_DEMO_B",
                "transaction_timestamp": "2022-09-03 12:00:00",
                "risk_score": "0.81",
                "risk_rank": "42",
                "model_name": "synthetic_xgboost",
                "model_version": "synthetic-v1",
            }
        ]
    )


class FakeQueryExecutor:
    """Return Athena-shaped synthetic results and record application queries."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def __call__(self, *, query: str) -> pd.DataFrame:
        self.queries.append(query)
        if f"FROM {BQ1_SCORE_TABLE}" in query:
            return synthetic_bq1_rows()
        if f"FROM {BQ2_EDGE_TABLE}" in query:
            return synthetic_bq2_rows()
        raise AssertionError("Unexpected query in offline demonstration.")


def main() -> None:
    handoff = AlertHandoff(
        transaction_id="txn_DemoAlert001",
        source_account_key="SYNTHETIC_DEMO_A",
        target_account_key="SYNTHETIC_DEMO_B",
        transaction_timestamp=pd.Timestamp(
            "2022-09-03T12:00:00Z"
        ).to_pydatetime(),
    )
    executor = FakeQueryExecutor()
    context = load_case_context(
        handoff=handoff,
        query_executor=executor,
        fetch_bq1=True,
        queried_window_hours=48,
    )
    payload = build_llm_input(
        context,
        max_candidates=20,
        max_transactions=100,
    )
    validate_case_context(context)
    validate_llm_input(payload)
    serialized = serialize_case_context(payload)

    print("Offline CaseContext integration demo passed")
    print(f"Schema version: {context['schema_version']}")
    print(f"Application-level queries: {len(executor.queries)} (BQ2=1, BQ1=1)")
    print(f"BQ1 status: {context['bq1']['status']}")
    print(
        "Evidence records: "
        f"{len(context['bq2']['transactions'])} transactions, "
        f"{len(context['bq2']['candidates'])} candidates"
    )
    print(f"Bounded payload JSON bytes: {len(serialized.encode('utf-8'))}")


if __name__ == "__main__":
    main()
