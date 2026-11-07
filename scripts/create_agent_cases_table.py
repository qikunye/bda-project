"""Create the DynamoDB table is459_agent_cases (key: day + case_id) if it does not exist.
Safe to run again. On-demand billing, tagged like is459_agent_decisions.
"""

import sys

import boto3

from src import config


def ensure_cases_table(client) -> str:
    """Returns "exists" or "created"."""

    try:
        client.describe_table(TableName=config.AGENT_CASES_TABLE)
        return "exists"
    except client.exceptions.ResourceNotFoundException:
        pass

    tag_key, tag_value = config.AGENT_TABLE_TAG
    client.create_table(
        TableName=config.AGENT_CASES_TABLE,
        AttributeDefinitions=[
            {"AttributeName": "day", "AttributeType": "S"},
            {"AttributeName": "case_id", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "day", "KeyType": "HASH"},
            {"AttributeName": "case_id", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
        Tags=[{"Key": tag_key, "Value": tag_value}],
    )
    client.get_waiter("table_exists").wait(TableName=config.AGENT_CASES_TABLE)
    return "created"


def main() -> int:
    client = boto3.client("dynamodb", region_name=config.AWS_REGION)
    print(f"{config.AGENT_CASES_TABLE}: {ensure_cases_table(client)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
