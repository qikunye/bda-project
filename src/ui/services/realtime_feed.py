"""Explicit, bounded discovery; no scans, object-body reads or pipeline execution."""

import re
from dataclasses import dataclass
from datetime import date
from itertools import islice

import boto3
from boto3.dynamodb.conditions import Key

from src import config
from src.ui.services.realtime import (
    ALERTS_TABLE,
    MAX_LOOKUPS,
    RealtimeRecord,
    RealtimeSchemaError,
    RealtimeService,
    _number,
    validate_ids,
)

MAX_RECORDS = 50
MAX_PAGES = 3
BRIEFS_BUCKET = "is459-aml-g1t5"


@dataclass(frozen=True)
class FeedResult:
    records: list[RealtimeRecord]
    limited: bool = False
    skipped: int = 0


def validate_request(day: date, limit: int) -> str:
    if type(day) is not date:
        raise ValueError("Choose a calendar date.")
    if type(limit) is not int or not 1 <= limit <= MAX_RECORDS:
        raise ValueError(f"Choose between 1 and {MAX_RECORDS} records.")
    return day.isoformat()


def explanation_txn(key: str, prefix: str) -> str | None:
    """Accept direct children only, following the writer's txn_<id>.json contract."""
    if not isinstance(key, str) or not key.startswith(prefix):
        return None
    match = re.fullmatch(r"(txn_[A-Za-z0-9]{1,252})\.json", key[len(prefix):])
    return match[1] if match else None


class RealtimeFeedService:
    """At most three discovery requests and 100 keyed reads per explicit load.

    Query uses a KEYS_ONLY index; canonical data is always mapped by lookup_many.
    Paginator page sizes become explicit Query Limit / ListObjectsV2 MaxKeys.
    """

    def __init__(self, *, index_name: str = "", dynamo=None, s3=None, lookup=None):
        self.index_name = index_name
        self._dynamo = dynamo
        self._s3 = s3
        self._lookup = lookup

    def _records(self, ids: list[str]) -> list[RealtimeRecord]:
        if not ids:
            return []
        if self._lookup is None:
            self._lookup = RealtimeService(self._dynamo)
        records = []
        for start in range(0, len(ids), MAX_LOOKUPS):
            records.extend(self._lookup.lookup_many(ids[start:start + MAX_LOOKUPS]))
        return records

    def recent(self, day: date, limit: int = 20) -> FeedResult:
        feed_day = validate_request(day, limit)
        if not self.index_name:
            raise ValueError("Recent-alert index is not configured. Use S3 discovery or manual lookup.")
        if self._dynamo is None:
            self._dynamo = boto3.resource("dynamodb", region_name=config.AWS_REGION)
        pages = self._dynamo.meta.client.get_paginator("query").paginate(
            TableName=ALERTS_TABLE, IndexName=self.index_name,
            KeyConditionExpression=Key("feed_day").eq(feed_day),
            ScanIndexForward=False, ConsistentRead=False,
            ProjectionExpression="txn, feed_day, scored_at_ms",
            PaginationConfig={"PageSize": limit, "MaxItems": MAX_PAGES * limit},
        )
        candidates = {}
        limited = False
        for page in islice(pages, MAX_PAGES):
            for item in page.get("Items", []):
                txn = item.get("txn")
                try:
                    if validate_ids([txn]) != [txn] or item.get("feed_day") != feed_day:
                        raise ValueError("Invalid feed identity.")
                    timestamp = _number(item, "scored_at_ms")
                    if timestamp is None:
                        raise ValueError("Missing processing timestamp.")
                except (ValueError, TypeError) as error:
                    raise RealtimeSchemaError("Index entries do not match the feed contract.") from error
                candidates[txn] = timestamp
            limited = bool(page.get("LastEvaluatedKey")) or len(candidates) > limit
            if len(candidates) >= limit:
                break
        ids = sorted(candidates, key=lambda txn: (-candidates[txn], txn))[:limit]
        return FeedResult(self._records(ids), limited)

    def completed(self, day: date, limit: int = 20) -> FeedResult:
        prefix = f"rt/briefs/dt={validate_request(day, limit)}/"
        if self._s3 is None:
            self._s3 = boto3.client("s3", region_name=config.AWS_REGION)
        pages = self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=BRIEFS_BUCKET, Prefix=prefix,
            PaginationConfig={"PageSize": limit, "MaxItems": MAX_PAGES * limit},
        )
        ids, skipped, limited = [], 0, False
        for page in islice(pages, MAX_PAGES):
            for item in page.get("Contents", []):
                txn = explanation_txn(item.get("Key"), prefix)
                if txn is None:
                    skipped += 1
                elif txn not in ids:
                    ids.append(txn)
            limited = bool(page.get("IsTruncated")) or len(ids) > limit
            if len(ids) >= limit:
                break
        return FeedResult(self._records(ids[:limit]), limited, skipped)
