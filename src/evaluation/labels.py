"""Reads ground-truth labels from Athena. This is the one place that names the label column."""

from collections.abc import Callable

import pandas as pd

LABEL_TABLE = "is459_aml_gold.bq1_risk_scores_medium_v2"
CHUNK = 1000

Runner = Callable[[str], tuple[pd.DataFrame, object]]


def _in_list(ids: list[str]) -> str:
    for value in ids:
        if not value.startswith("txn_") or not value[4:].isalnum():
            raise ValueError(f"Invalid transaction ID: {value!r}")
    return ", ".join(f"'{v}'" for v in ids)


def fetch_labels(run: Runner, txn_ids: list[str]) -> tuple[dict[str, bool], list[dict]]:
    """Returns ``({txn_id: is_laundering}, scored_rows)``. Scored rows are for pattern matching."""

    labels: dict[str, bool] = {}
    rows: list[dict] = []
    for start in range(0, len(txn_ids), CHUNK):
        chunk = txn_ids[start : start + CHUNK]
        frame, _ = run(
            "SELECT transaction_id, source_account_key, target_account_key, "
            "transaction_timestamp, evaluation_is_laundering "
            f"FROM {LABEL_TABLE} WHERE transaction_id IN ({_in_list(chunk)})"
        )
        for record in frame.to_dict("records"):
            labels[record["transaction_id"]] = str(record["evaluation_is_laundering"]) in ("1", "True", "true")
            rows.append(record)
    return labels, rows
