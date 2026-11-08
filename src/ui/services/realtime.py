"""Bounded keyed reads using PLAN.md F21; no chronological listing contract is available."""

from dataclasses import dataclass
from typing import Literal

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src import config
from src.ui.services.errors import ServiceFailure, describe_failure

ALERTS_TABLE = "is459_rt_alerts"
BRIEFS_TABLE = "is459_rt_briefs"
MAX_LOOKUPS = 20

RealtimeStatus = Literal["available", "pending", "not_found", "alert_error", "brief_error",
                         "incompatible", "unpaired"]
STATUS_LABELS = {
    "available": "Brief record available",
    "pending": "Brief not yet stored / pending",
    "not_found": "Alert not found in this lookup",
    "alert_error": "Alert read failed",
    "brief_error": "Brief read failed",
    "incompatible": "Schema incompatible",
    "unpaired": "Brief found without matching alert",
}


@dataclass(frozen=True)
class AlertSummary:
    txn: str
    amount: str | None = None
    # Presentation fields only: live field names for these have not been verified.
    timestamp: str | None = None
    sender: str | None = None
    receiver: str | None = None
    currency: str | None = None
    score: float | None = None


@dataclass(frozen=True)
class Explanation:
    txn: str
    narrative: str | None = None
    next_checks: tuple[str, ...] = ()
    typology: str | None = None
    completed_at: str | None = None
    content_mapped: bool = False


@dataclass(frozen=True)
class RealtimeRecord:
    txn: str
    status: RealtimeStatus
    alert: AlertSummary | None = None
    brief: Explanation | None = None
    detail: str = ""
    alert_failure: ServiceFailure | None = None
    brief_failure: ServiceFailure | None = None


class RealtimeSchemaError(ValueError):
    """A record/key cannot be matched safely to the documented minimal contract."""


def validate_ids(txn_ids: list[str]) -> list[str]:
    if not txn_ids or len(txn_ids) > MAX_LOOKUPS:
        raise ValueError(f"Provide between 1 and {MAX_LOOKUPS} known transaction IDs.")
    if any(not isinstance(txn, str) or not txn.strip() or len(txn) > 256 or not txn.isprintable()
           for txn in txn_ids):
        raise ValueError("Each transaction ID must be a nonempty printable string of at most 256 characters.")
    return list(dict.fromkeys(txn.strip() for txn in txn_ids))


def _identity(item: dict | None, txn: str) -> None:
    if item is not None and (not isinstance(item, dict) or item.get("txn") != txn):
        raise RealtimeSchemaError("The record does not match the requested txn key.")


def map_documented_alert(item: dict | None, txn: str) -> AlertSummary | None:
    """Only txn and amount are documented with exact field names (PLAN.md F21)."""
    _identity(item, txn)
    if item is None:
        return None
    amount = item.get("amount")
    if amount is not None and not isinstance(amount, str):
        raise RealtimeSchemaError("The documented alert amount must be a string when present.")
    return AlertSummary(txn, amount=amount)


def map_documented_brief(item: dict | None, txn: str) -> Explanation | None:
    """Do not guess narrative/next-check/typology attribute names or expose raw records."""
    _identity(item, txn)
    return Explanation(txn) if item is not None else None


class RealtimeService:
    """Only GetItem, at most two keyed reads per supplied ID. No index/scan fallback."""

    def __init__(self, dynamo=None):
        self._dynamo = dynamo

    def _read(self, table: str, txn: str):
        if self._dynamo is None:
            self._dynamo = boto3.resource("dynamodb", region_name=config.AWS_REGION)
        return self._dynamo.Table(table).get_item(Key={"txn": txn}, ConsistentRead=True).get("Item")

    def lookup(self, txn: str) -> RealtimeRecord:
        txn = validate_ids([txn])[0]
        items, failures, schema_error = {}, {}, False
        for side, table in (("alert", ALERTS_TABLE), ("brief", BRIEFS_TABLE)):
            try:
                items[side] = self._read(table, txn)
            except (BotoCoreError, ClientError, OSError) as error:
                if isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") == "ValidationException":
                    schema_error = True
                failures[side] = describe_failure(error, f"Real-time {side} lookup")
        try:
            alert = map_documented_alert(items.get("alert"), txn)
            brief = map_documented_brief(items.get("brief"), txn)
        except RealtimeSchemaError:
            schema_error = True
            alert, brief = None, None
        if schema_error:
            status = "incompatible"
        elif "alert" in failures:
            status = "alert_error"
        elif "brief" in failures:
            status = "brief_error"
        elif alert is None:
            status = "unpaired" if brief is not None else "not_found"
        else:
            status = "available" if brief is not None else "pending"
        return RealtimeRecord(txn, status, alert, brief,
                              alert_failure=failures.get("alert"), brief_failure=failures.get("brief"))

    def lookup_many(self, txn_ids: list[str]) -> list[RealtimeRecord]:
        return [self.lookup(txn) for txn in validate_ids(txn_ids)]
