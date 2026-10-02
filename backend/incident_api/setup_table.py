"""Creates the DynamoDB table if it doesn't exist.  Usage: python -m incident_api.setup_table

Table layout (single table, items grouped by incident):
    pk = "INCIDENT#<id>", sk = "META"                  the incident itself
    pk = "INCIDENT#<id>", sk = "EVENT#<time>#<rand>"   its timeline, in time order
    pk = "COUNTER",       sk = "INCIDENT"              last issued incident number
GSI "by-created" (gsi1pk = "INCIDENT", gsi1sk = "<created_at>#<id>") lists incidents newest first.
"""

import boto3

from incident_api import config

TABLE_SCHEMA = {
    "AttributeDefinitions": [
        {"AttributeName": "pk", "AttributeType": "S"},
        {"AttributeName": "sk", "AttributeType": "S"},
        {"AttributeName": "gsi1pk", "AttributeType": "S"},
        {"AttributeName": "gsi1sk", "AttributeType": "S"},
    ],
    "KeySchema": [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ],
    "GlobalSecondaryIndexes": [
        {
            "IndexName": "by-created",
            "KeySchema": [
                {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ],
    "BillingMode": "PAY_PER_REQUEST",
}


def create_table(client, table_name: str) -> bool:
    """Create the table and wait until it's ready. Returns False if it already existed."""
    try:
        client.create_table(TableName=table_name, **TABLE_SCHEMA)
    except client.exceptions.ResourceInUseException:
        return False
    client.get_waiter("table_exists").wait(TableName=table_name)
    return True


if __name__ == "__main__":
    session = boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.AWS_REGION)
    dynamodb = session.client("dynamodb")
    created = create_table(dynamodb, config.TABLE_NAME)
    print(f"{config.TABLE_NAME}: {'created' if created else 'already exists'}")
