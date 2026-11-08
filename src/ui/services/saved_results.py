"""Keyed DynamoDB and S3 reads only; never scans, publishes or executes an agent."""

import json
import time
from dataclasses import dataclass, field

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src import config
from src.ui.services.contracts import (
    IncompatibleResult,
    native,
    validate_decision,
    validate_saved,
)

STATUS_LABELS = {
    "missing": "No saved brief",
    "saved": "Saved brief",
    "empty": "Saved · no findings",
    "fallback": "Saved · code-only fallback",
    "error": "Read failed",
    "incompatible": "Incompatible saved data",
}


@dataclass
class DecisionBatch:
    rows: dict[str, dict] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class SavedBrief:
    status: str
    detail: str = ""
    result: dict | None = None
    run_id: str | None = None
    created_at: str | None = None
    scenario: str | None = None


def decision_state(txn_id: str, day: str, batch: DecisionBatch) -> tuple[str, dict | None]:
    if txn_id in batch.errors:
        return "error", None
    selected = batch.rows.get(txn_id)
    if selected is None:
        return "missing", None
    try:
        validate_decision(selected, day)
        rep_id = selected["representative_txn_id"]
        if rep_id in batch.errors or rep_id not in batch.rows:
            return "error", None
        representative = batch.rows[rep_id]
        validate_decision(representative, day)
        if representative["role"] != "representative" or any(
            selected[key] != representative[key] for key in ("run_id", "created_at", "case_id")
        ):
            return "incompatible", None
    except (IncompatibleResult, KeyError):
        return "incompatible", None
    status = "fallback" if representative["fallback"] else (
        "saved" if representative["finding_codes"] else "empty"
    )
    return status, representative


class SavedResultsReader:
    """Lazy, injectable resource clients. A missing key is distinct from a failed read."""

    def __init__(self, dynamo=None, s3=None, *, sleep=time.sleep, max_retries: int = 3):
        self._dynamo = dynamo
        self._s3 = s3
        self.sleep = sleep
        self.max_retries = max_retries

    @property
    def dynamo(self):
        if self._dynamo is None:
            self._dynamo = boto3.resource("dynamodb", region_name=config.AWS_REGION)
        return self._dynamo

    def read_decisions(self, txn_ids: list[str]) -> DecisionBatch:
        batch = DecisionBatch()
        ids = list(dict.fromkeys(txn_ids))
        for start in range(0, len(ids), 100):
            pending = [{"transaction_id": txn_id} for txn_id in ids[start:start + 100]]
            for attempt in range(self.max_retries + 1):
                try:
                    response = self.dynamo.batch_get_item(RequestItems={
                        config.AGENT_DECISIONS_TABLE: {"Keys": pending, "ConsistentRead": True},
                    })
                    for row in response.get("Responses", {}).get(config.AGENT_DECISIONS_TABLE, []):
                        batch.rows[row["transaction_id"]] = native(row)
                    pending = response.get("UnprocessedKeys", {}).get(
                        config.AGENT_DECISIONS_TABLE, {}
                    ).get("Keys", [])
                except (BotoCoreError, ClientError, OSError, KeyError, TypeError):
                    for key in pending:
                        batch.errors[key["transaction_id"]] = "Decision read failed; reload the queue."
                    break
                if not pending:
                    break
                if attempt == self.max_retries:
                    for key in pending:
                        batch.errors[key["transaction_id"]] = "Decision read remained unprocessed."
                else:
                    self.sleep(0.1 * 2 ** attempt)
        return batch

    def resolve_decisions(self, txn_ids: list[str]) -> DecisionBatch:
        batch = self.read_decisions(txn_ids)
        representatives = list({row["representative_txn_id"] for row in batch.rows.values()
                                if isinstance(row.get("representative_txn_id"), str)}
                               - set(batch.rows) - {""})
        if representatives:
            extra = self.read_decisions(representatives)
            batch.rows.update(extra.rows)
            batch.errors.update(extra.errors)
        return batch

    def read_case(self, day: str, case_id: str) -> dict | None:
        return native(self.dynamo.Table(config.AGENT_CASES_TABLE).get_item(
            Key={"day": day, "case_id": case_id}, ConsistentRead=True,
        ).get("Item"))

    def read_trace(self, key: str) -> dict:
        if self._s3 is None:
            self._s3 = boto3.client("s3", region_name=config.AWS_REGION)
        with self._s3.get_object(Bucket=config.AGENT_TRACE_BUCKET, Key=key)["Body"] as body:
            return json.loads(body.read())

    def open(self, txn_id: str, day: str, batch: DecisionBatch) -> SavedBrief:
        status, representative = decision_state(txn_id, day, batch)
        if representative is None:
            return SavedBrief(status, STATUS_LABELS[status])
        try:
            case = self.read_case(day, f"case_{representative['transaction_id']}")
            if case is None:
                return SavedBrief("error", "Saved case metadata is missing; reload the queue.")
            envelope = self.read_trace(representative["trace_key"])
            result = validate_saved(batch.rows[txn_id], representative, case, envelope)
        except (IncompatibleResult, ValueError, TypeError, KeyError):
            return SavedBrief("incompatible", "Saved records are incompatible or inconsistent; reload the queue.")
        except (BotoCoreError, ClientError, OSError):
            return SavedBrief("error", "Saved case or trace could not be read; reload the queue.")
        return SavedBrief(status, result=result, run_id=envelope["run_id"], created_at=envelope["created_at"])
