"""Saves batch results to two DynamoDB tables and run traces to S3. These are the only places the
agent writes to.
"""

import json
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

from src import config


def to_dynamo(item: dict) -> dict:
    """DynamoDB rejects Python floats, so turn them into Decimals."""

    return json.loads(json.dumps(item), parse_float=Decimal)


class DynamoStore:
    """Reads and writes ``is459_agent_decisions`` and ``is459_agent_cases``."""

    def __init__(
        self,
        resource=None,
        *,
        decisions_table: str = config.AGENT_DECISIONS_TABLE,
        cases_table: str = config.AGENT_CASES_TABLE,
    ):
        resource = resource or boto3.resource("dynamodb", region_name=config.AWS_REGION)
        self._decisions = resource.Table(decisions_table)
        self._cases = resource.Table(cases_table)

    def put_decisions(self, rows: list[dict]) -> None:
        with self._decisions.batch_writer() as batch:
            for row in rows:
                batch.put_item(Item=to_dynamo(row))

    def put_cases(self, rows: list[dict]) -> None:
        with self._cases.batch_writer() as batch:
            for row in rows:
                batch.put_item(Item=to_dynamo(row))

    def decision_keys_for_day(self, day: str) -> set[str]:
        """Transaction IDs of every decision row for a day. The table has no day index, so scan."""

        keys: set[str] = set()
        request = {
            "FilterExpression": Attr("day").eq(day),
            "ProjectionExpression": "transaction_id",
        }
        while True:
            page = self._decisions.scan(**request)
            keys.update(item["transaction_id"] for item in page["Items"])
            if "LastEvaluatedKey" not in page:
                return keys
            request["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def case_ids_for_day(self, day: str) -> set[str]:
        ids: set[str] = set()
        request = {
            "KeyConditionExpression": Key("day").eq(day),
            "ProjectionExpression": "case_id",
        }
        while True:
            page = self._cases.query(**request)
            ids.update(item["case_id"] for item in page["Items"])
            if "LastEvaluatedKey" not in page:
                return ids
            request["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def delete_decisions(self, txn_ids: set[str]) -> None:
        with self._decisions.batch_writer() as batch:
            for txn_id in sorted(txn_ids):
                batch.delete_item(Key={"transaction_id": txn_id})

    def delete_cases(self, day: str, case_ids: set[str]) -> None:
        with self._cases.batch_writer() as batch:
            for case_id in sorted(case_ids):
                batch.delete_item(Key={"day": day, "case_id": case_id})


class S3TraceStore:
    """Saves one JSON trace per representative under ``agent/traces/<day>/``."""

    def __init__(self, client=None, *, bucket: str = config.AGENT_TRACE_BUCKET):
        self._client = client or boto3.client("s3", region_name=config.AWS_REGION)
        self._bucket = bucket

    def put(self, key: str, body: dict) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(body, sort_keys=True, default=str).encode("utf-8"),
            ContentType="application/json",
        )

    def keys(self, prefix: str) -> set[str]:
        found: set[str] = set()
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            found.update(obj["Key"] for obj in page.get("Contents", []))
        return found

    def delete(self, keys: set[str]) -> None:
        ordered = sorted(keys)
        for start in range(0, len(ordered), 1000):  # S3 allows 1,000 keys per request.
            self._client.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": k} for k in ordered[start : start + 1000]]},
            )
