"""Creates the DynamoDB table, or adds any missing indexes to an existing one.

Usage: python -m incident_api.setup_table   (safe to run repeatedly)

Table layout (single table, items grouped by incident):
    pk = "INCIDENT#<id>", sk = "META"                  the incident itself
    pk = "INCIDENT#<id>", sk = "EVENT#<time>#<rand>"   its timeline, in time order
    pk = "COUNTER",       sk = "INCIDENT"              last issued incident number
Indexes (both sort newest first by gsi1sk = "<created_at>#<id>"):
    "by-created"         gsi1pk = "INCIDENT" on every incident
    "active-by-created"  active_pk = "ACTIVE" only while the incident is not RESOLVED
                         (a sparse index: resolved incidents drop out of it entirely)
"""

import time

import boto3

from incident_api import config

ATTRIBUTES = [
    {"AttributeName": name, "AttributeType": "S"}
    for name in ("pk", "sk", "gsi1pk", "gsi1sk", "active_pk")
]


def _index(name: str, partition_key: str) -> dict:
    return {
        "IndexName": name,
        "KeySchema": [
            {"AttributeName": partition_key, "KeyType": "HASH"},
            {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }


INDEXES = [_index("by-created", "gsi1pk"), _index("active-by-created", "active_pk")]


def ensure_table(client, table_name: str) -> str:
    """Make the table match this schema. Returns a short description of what was done."""
    try:
        client.create_table(
            TableName=table_name,
            AttributeDefinitions=ATTRIBUTES,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=INDEXES,
            BillingMode="PAY_PER_REQUEST",
        )
    except client.exceptions.ResourceInUseException:
        return _add_missing_indexes(client, table_name)
    client.get_waiter("table_exists").wait(TableName=table_name)
    return "created"


def _add_missing_indexes(client, table_name: str) -> str:
    table = client.describe_table(TableName=table_name)["Table"]
    existing = {i["IndexName"] for i in table.get("GlobalSecondaryIndexes", [])}
    missing = [i for i in INDEXES if i["IndexName"] not in existing]
    for index in missing:  # DynamoDB allows one index creation per update
        client.update_table(
            TableName=table_name,
            AttributeDefinitions=ATTRIBUTES,
            GlobalSecondaryIndexUpdates=[{"Create": index}],
        )
        _wait_for_index(client, table_name, index["IndexName"])
    if missing:
        return "added indexes: " + ", ".join(i["IndexName"] for i in missing)
    return "already up to date"


def _wait_for_index(client, table_name: str, index_name: str, timeout_s: float = 600) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        indexes = client.describe_table(TableName=table_name)["Table"]["GlobalSecondaryIndexes"]
        if any(i["IndexName"] == index_name and i["IndexStatus"] == "ACTIVE" for i in indexes):
            return
        time.sleep(5)
    raise TimeoutError(f"index {index_name} did not become active")


if __name__ == "__main__":
    session = boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.AWS_REGION)
    print(f"{config.TABLE_NAME}: {ensure_table(session.client('dynamodb'), config.TABLE_NAME)}")
