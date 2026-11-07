from scripts.create_agent_cases_table import ensure_cases_table


class Missing(Exception):
    pass


class FakeDynamo:
    def __init__(self, exists):
        self.exists = exists
        self.created = None
        self.waited = None

        class Exceptions:
            ResourceNotFoundException = Missing

        self.exceptions = Exceptions

    def describe_table(self, TableName):
        if not self.exists:
            raise Missing()
        return {}

    def create_table(self, **kwargs):
        self.created = kwargs

    def get_waiter(self, name):
        client = self

        class Waiter:
            def wait(self, TableName):
                client.waited = (name, TableName)

        return Waiter()


def test_a_missing_table_is_created_with_the_day_and_case_key_and_the_project_tag():
    client = FakeDynamo(exists=False)
    assert ensure_cases_table(client) == "created"
    assert client.created["TableName"] == "is459_agent_cases"
    assert client.created["KeySchema"] == [
        {"AttributeName": "day", "KeyType": "HASH"},
        {"AttributeName": "case_id", "KeyType": "RANGE"},
    ]
    assert client.created["BillingMode"] == "PAY_PER_REQUEST"
    assert client.created["Tags"] == [{"Key": "project", "Value": "is459-g1t5-etl-v2"}]
    assert client.waited == ("table_exists", "is459_agent_cases")


def test_an_existing_table_is_left_alone():
    client = FakeDynamo(exists=True)
    assert ensure_cases_table(client) == "exists"
    assert client.created is None
