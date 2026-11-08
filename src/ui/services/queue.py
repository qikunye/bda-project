"""BQ1 queue enrichment, retaining BQ1 order and every loaded alert."""

from dataclasses import dataclass

import pandas as pd

from src import config
from src.agent import tools
from src.agent.data import AgentData, athena_runner
from src.agent.review_list import build_review_list
from src.ui.services.contracts import label_free
from src.ui.services.saved_results import (
    STATUS_LABELS,
    DecisionBatch,
    SavedResultsReader,
    decision_state,
)

QUEUE_COLUMNS = ["transaction_id", "transaction_timestamp", "source_account_key",
                 "target_account_key", "risk_score", "risk_rank_day", "risk_rank_global"]


@dataclass
class QueueSnapshot:
    day: str
    top_k: int
    entries: list[dict]
    decisions: DecisionBatch


def enrich_queue(frame: pd.DataFrame, batch: DecisionBatch, day: str) -> list[dict]:
    """Attach only valid metadata. Failed reads do not silently become missing briefs."""
    if frame.empty:
        return []
    label_free(frame.to_dict("records"))
    if "risk_score" not in frame.columns:
        frame = frame.assign(risk_score=None)
    frame = frame[QUEUE_COLUMNS].copy()
    frame["risk_rank_day"] = pd.to_numeric(frame["risk_rank_day"], errors="raise")
    if frame["transaction_id"].duplicated().any() or frame["risk_rank_day"].isna().any():
        raise ValueError("Daily queue has duplicate alerts or missing ranks.")
    frame = frame.sort_values("risk_rank_day", kind="stable")
    ids = frame["transaction_id"].tolist()
    states = {txn_id: decision_state(txn_id, day, batch) for txn_id in ids}
    valid_rows = [row for txn_id, row in batch.rows.items()
                  if decision_state(txn_id, day, batch)[0] in {"saved", "empty", "fallback"}]
    reviews = build_review_list(ids, valid_rows)
    entries = []
    for record, review in zip(frame.to_dict("records"), reviews):
        state, rep = states[record["transaction_id"]]
        entries.append({
            **record, **review,
            "status": state,
            "Brief availability": STATUS_LABELS[state],
            "Finding count": len(rep["finding_codes"]) if rep is not None else None,
            "Finding codes": ", ".join(rep["finding_codes"]) if rep is not None else "",
        })
    return entries


class LiveBatchService:
    """Create AWS access only after an explicit live queue request."""

    def __init__(self, data=None, saved=None):
        self.data = data
        self.saved = saved if saved is not None else SavedResultsReader()

    def load_queue(self, day: str, top_k: int) -> QueueSnapshot:
        if day not in config.AGENT_SCORED_DATES:
            raise ValueError("Unsupported analysis date.")
        if type(top_k) is not int or not 1 <= top_k <= config.AGENT_QUEUE_MAX_K:
            raise ValueError("Review selection exceeds the available daily queue.")
        if self.data is None:
            self.data = AgentData(athena_runner())
        frame = tools.load_day_queue(self.data, day, top_k)
        ids = frame["transaction_id"].tolist() if not frame.empty else []
        batch = self.saved.resolve_decisions(ids)
        return QueueSnapshot(day, top_k, enrich_queue(frame, batch, day), batch)

    def open(self, snapshot: QueueSnapshot, txn_id: str):
        return self.saved.open(txn_id, snapshot.day, snapshot.decisions)
