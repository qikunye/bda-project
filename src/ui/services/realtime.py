"""Bounded keyed reads mapped from rt/code writer exports and sampled DynamoDB items."""

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
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
    # txn_ts is the dataset transaction time; scoring/arrival timestamps are separate.
    timestamp: str | None = None
    sender: str | None = None
    receiver: str | None = None
    currency: str | None = None
    score: float | None = None
    payment_format: str | None = None
    model_version: str | None = None
    scored_at: str | None = None
    arrival_at: str | None = None
    ingest_to_score_ms: float | None = None


@dataclass(frozen=True)
class Explanation:
    txn: str
    narrative: str | None = None
    next_checks: tuple[str, ...] = ()
    typology: str | None = None
    completed_at: str | None = None
    content_mapped: bool = False
    pipeline_status: str | None = None
    model_version: str | None = None
    latency_ms: float | None = None


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


def _text(item: dict, key: str) -> str | None:
    value = item.get(key)
    if value is not None and not isinstance(value, str):
        raise RealtimeSchemaError(f"{key} must be a string when present.")
    return value


def _number(item: dict, key: str) -> float | None:
    value = item.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or not math.isfinite(value):
        raise RealtimeSchemaError(f"{key} must be a finite DynamoDB number when present.")
    return float(value)


def _epoch_ms(item: dict, key: str) -> str | None:
    value = _number(item, key)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError, OSError) as error:
        raise RealtimeSchemaError("Stored epoch timestamp is outside the supported range.") from error


def map_documented_alert(item: dict | None, txn: str) -> AlertSummary | None:
    """Map app.py's base/ALERTS item; do not read model reasons, ranks or raw features."""
    _identity(item, txn)
    if item is None:
        return None
    return AlertSummary(
        txn, amount=_text(item, "amount"), timestamp=_text(item, "txn_ts"),
        sender=_text(item, "sender"), receiver=_text(item, "receiver"), currency=_text(item, "currency"),
        score=_number(item, "score"), payment_format=_text(item, "payment_format"),
        model_version=_text(item, "model_version"), scored_at=_epoch_ms(item, "scored_at_ms"),
        arrival_at=_epoch_ms(item, "arrival_ms"), ingest_to_score_ms=_number(item, "ingest_to_score_ms"),
    )


def map_documented_brief(item: dict | None, txn: str) -> Explanation | None:
    """Map explainer_app.py's DynamoDB item; next_checks is S(JSON), not a native list."""
    _identity(item, txn)
    if item is None:
        return None
    raw_checks = _text(item, "next_checks")
    try:
        checks = json.loads(raw_checks) if raw_checks is not None else []
    except ValueError as error:
        raise RealtimeSchemaError("Stored next_checks is not valid JSON.") from error
    if not isinstance(checks, list) or not all(isinstance(check, str) for check in checks):
        raise RealtimeSchemaError("Stored next_checks must encode a list of strings.")
    status = _text(item, "status")
    if status is not None and status not in {"ok", "needs_review"}:
        raise RealtimeSchemaError("Unknown explainer processing status.")
    return Explanation(
        txn, narrative=_text(item, "narrative"), next_checks=tuple(checks), typology=_text(item, "typology"),
        # written_at_ms exists in the S3 JSON, but the DynamoDB writer does not persist it.
        completed_at=None, content_mapped=bool({"narrative", "next_checks", "typology", "status"} & item.keys()),
        pipeline_status=status, model_version=_text(item, "model_version"), latency_ms=_number(item, "latency_ms"),
    )


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
