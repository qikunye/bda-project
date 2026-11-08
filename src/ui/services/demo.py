"""In-memory queue and persistence envelopes built from unchanged agent fixtures."""

from copy import deepcopy

import pandas as pd

from src import config
from src.agent.sample_results import (
    SAMPLE_CREATED_AT,
    SAMPLE_RUN_ID,
    sample_decision_rows,
    sample_investigations,
)
from src.ui.services.queue import QueueSnapshot, enrich_queue
from src.ui.services.saved_results import DecisionBatch, SavedResultsReader


class DemoBatchService(SavedResultsReader):
    """All data stays in memory; inherited open() checks the same saved-run identities."""

    def __init__(self):
        super().__init__()
        self.results = deepcopy(sample_investigations())
        self.rows = {row["transaction_id"]: deepcopy(row) for row in sample_decision_rows()}
        self.cases = {}
        self.envelopes = {}
        self.scenarios = {}
        self.alerts = []
        for scenario, result in self.results.items():
            if result["status"] != "ok":
                continue
            # quiet_truncated's fixture request date is 14 Sep but its alert is 16 Sep.
            # Normalise only this in-memory envelope so its daily queue uses the actual alert date.
            result["txn_date"] = result["alert"]["txn_date"]
            representative = self.rows[result["txn_id"]]
            representative["day"] = result["txn_date"]
            member_ids = [member["alert"]["txn_id"] for member in result["members"]]
            for member in result["members"]:
                alert = member["alert"]
                self.rows[alert["txn_id"]]["day"] = result["txn_date"]
                self.alerts.append(deepcopy(alert))
                self.scenarios[alert["txn_id"]] = scenario
            case_id = f"case_{result['txn_id']}"
            self.cases[(result["txn_date"], case_id)] = {
                "day": result["txn_date"], "case_id": case_id,
                "representative_txn_id": result["txn_id"], "member_txn_ids": member_ids,
                "size": len(member_ids), "finding_codes": representative["finding_codes"],
                "fallback": result["fallback"], "run_id": SAMPLE_RUN_ID,
                "created_at": SAMPLE_CREATED_AT, "trace_key": representative["trace_key"],
            }
            self.envelopes[representative["trace_key"]] = {
                "run_id": SAMPLE_RUN_ID, "created_at": SAMPLE_CREATED_AT, "result": result,
            }
        self.alerts.append({
            "txn_id": "txn_demounprocessed", "txn_date": "2022-09-14",
            "ts": "2022-09-14 15:00:00", "sender": "700|DEMOONLY", "receiver": "800|DEMOONLY",
            "risk_score": 0.25, "risk_rank_day": 40, "risk_rank_global": 400,
        })

    def read_decisions(self, txn_ids: list[str]) -> DecisionBatch:
        return DecisionBatch({txn_id: deepcopy(self.rows[txn_id]) for txn_id in txn_ids if txn_id in self.rows})

    def read_case(self, day: str, case_id: str):
        return deepcopy(self.cases.get((day, case_id)))

    def read_trace(self, key: str):
        return deepcopy(self.envelopes[key])

    def load_queue(self, day: str, top_k: int) -> QueueSnapshot:
        if day not in config.AGENT_SCORED_DATES or not 1 <= top_k <= config.AGENT_QUEUE_MAX_K:
            raise ValueError("Unsupported demo queue selection.")
        frame = pd.DataFrame([
            {
                "transaction_id": alert["txn_id"], "transaction_timestamp": alert["ts"],
                "source_account_key": alert["sender"], "target_account_key": alert["receiver"],
                "risk_score": alert["risk_score"], "risk_rank_day": alert["risk_rank_day"],
                "risk_rank_global": alert["risk_rank_global"],
            }
            for alert in self.alerts if alert["txn_date"] == day
        ])
        if not frame.empty:
            frame = frame.sort_values("risk_rank_day", kind="stable").head(top_k)
        ids = frame["transaction_id"].tolist() if not frame.empty else []
        batch = self.resolve_decisions(ids)
        return QueueSnapshot(day, top_k, enrich_queue(frame, batch, day), batch)

    def open(self, snapshot: QueueSnapshot, txn_id: str):
        saved = super().open(txn_id, snapshot.day, snapshot.decisions)
        saved.scenario = self.scenarios.get(txn_id)
        return saved
