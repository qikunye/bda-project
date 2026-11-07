from decimal import Decimal

from boto3.dynamodb.conditions import Attr, Key

from src.agent.store import DynamoStore, S3TraceStore, to_dynamo


class Batch:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def put_item(self, Item):
        self.log.append(("put", Item))

    def delete_item(self, Key):
        self.log.append(("delete", Key))


class FakeTable:
    def __init__(self, pages=()):
        self.log = []
        self.requests = []
        self._pages = list(pages)

    def batch_writer(self):
        return Batch(self.log)

    def _next(self, **request):
        self.requests.append(request)
        return self._pages.pop(0)

    scan = query = _next


class FakeResource:
    def __init__(self, tables):
        self._tables = tables

    def Table(self, name):
        return self._tables[name]


def store(decisions=None, cases=None):
    decisions, cases = decisions or FakeTable(), cases or FakeTable()
    return (
        DynamoStore(
            FakeResource({"d": decisions, "c": cases}),
            decisions_table="d",
            cases_table="c",
        ),
        decisions,
        cases,
    )


def test_floats_become_decimals_and_other_values_are_kept():
    item = to_dynamo({"a": 0.5, "b": [1, {"c": 2.25}], "d": None, "e": "x"})
    assert item == {
        "a": Decimal("0.5"),
        "b": [1, {"c": Decimal("2.25")}],
        "d": None,
        "e": "x",
    }


def test_rows_are_written_to_the_right_tables():
    dynamo, decisions, cases = store()
    dynamo.put_decisions([{"transaction_id": "txn_a", "x": 1.5}])
    dynamo.put_cases([{"day": "d", "case_id": "c"}])
    assert decisions.log == [("put", {"transaction_id": "txn_a", "x": Decimal("1.5")})]
    assert cases.log == [("put", {"day": "d", "case_id": "c"})]


def test_decision_keys_for_a_day_follow_every_page_of_the_scan():
    pages = [
        {
            "Items": [{"transaction_id": "txn_a"}],
            "LastEvaluatedKey": {"transaction_id": "txn_a"},
        },
        {"Items": [{"transaction_id": "txn_b"}]},
    ]
    dynamo, decisions, _ = store(decisions=FakeTable(pages))
    assert dynamo.decision_keys_for_day("2022-09-14") == {"txn_a", "txn_b"}
    assert decisions.requests[0]["FilterExpression"] == Attr("day").eq("2022-09-14")
    assert decisions.requests[0]["ProjectionExpression"] == "transaction_id"
    assert decisions.requests[1]["ExclusiveStartKey"] == {"transaction_id": "txn_a"}


def test_case_ids_for_a_day_use_the_key_and_follow_pages():
    pages = [
        {
            "Items": [{"case_id": "c1"}],
            "LastEvaluatedKey": {"day": "d", "case_id": "c1"},
        },
        {"Items": [{"case_id": "c2"}]},
    ]
    dynamo, _, cases = store(cases=FakeTable(pages))
    assert dynamo.case_ids_for_day("d") == {"c1", "c2"}
    assert cases.requests[0]["KeyConditionExpression"] == Key("day").eq("d")


def test_deletes_use_the_full_key():
    dynamo, decisions, cases = store()
    dynamo.delete_decisions({"txn_b", "txn_a"})
    dynamo.delete_cases("d", {"c2", "c1"})
    assert decisions.log == [
        ("delete", {"transaction_id": "txn_a"}),
        ("delete", {"transaction_id": "txn_b"}),
    ]
    assert cases.log == [
        ("delete", {"day": "d", "case_id": "c1"}),
        ("delete", {"day": "d", "case_id": "c2"}),
    ]


class FakeS3:
    def __init__(self, keys=()):
        self.objects = {k: b"" for k in keys}
        self.delete_calls = []

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for k in client.objects if k.startswith(Prefix))
                yield {"Contents": [{"Key": k} for k in keys[:2]]} if keys else {}
                if len(keys) > 2:
                    yield {"Contents": [{"Key": k} for k in keys[2:]]}

        return Paginator()

    def delete_objects(self, Bucket, Delete):
        keys = [o["Key"] for o in Delete["Objects"]]
        self.delete_calls.append(keys)
        for k in keys:
            del self.objects[k]


def test_traces_are_saved_as_json():
    client = FakeS3()
    S3TraceStore(client, bucket="b").put("agent/traces/d/t.json", {"b": 1, "a": 2.5})
    assert client.objects["agent/traces/d/t.json"] == b'{"a": 2.5, "b": 1}'


def test_keys_lists_every_page_under_a_prefix():
    client = FakeS3(
        [
            "agent/traces/d/1.json",
            "agent/traces/d/2.json",
            "agent/traces/d/3.json",
            "agent/traces/e/1.json",
        ]
    )
    assert S3TraceStore(client, bucket="b").keys("agent/traces/d/") == {
        "agent/traces/d/1.json",
        "agent/traces/d/2.json",
        "agent/traces/d/3.json",
    }
    assert S3TraceStore(FakeS3(), bucket="b").keys("agent/traces/d/") == set()


def test_deletes_go_in_requests_of_at_most_1000_keys():
    keys = {f"agent/traces/d/{i:05}.json" for i in range(2500)}
    client = FakeS3(keys)
    S3TraceStore(client, bucket="b").delete(keys)
    assert [len(call) for call in client.delete_calls] == [1000, 1000, 500]
    assert client.objects == {}
