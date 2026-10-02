"""Schema upgrades must preserve the active list for incidents created earlier."""

import boto3
from moto import mock_aws

from incident_api.models import IncidentCreate, Status
from incident_api.setup_table import ATTRIBUTES, INDEXES, ensure_table
from incident_api.store import IncidentStore


def test_existing_table_index_upgrade_backfills_open_and_acknowledged(monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-2")
        client.create_table(
            TableName="legacy-incidents",
            AttributeDefinitions=[a for a in ATTRIBUTES if a["AttributeName"] != "active_pk"],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[INDEXES[0]],
            BillingMode="PAY_PER_REQUEST",
        )
        table = boto3.resource("dynamodb", region_name="us-east-2").Table("legacy-incidents")
        store = IncidentStore(table)
        opened = store.create(IncidentCreate(title="old open", service="payment"))
        acknowledged = store.create(IncidentCreate(title="old acknowledged", service="payment"))
        resolved = store.create(IncidentCreate(title="old resolved", service="payment"))
        store.update(acknowledged.incident_id, {"status": Status.ACKNOWLEDGED}, [], None)
        store.update(resolved.incident_id, {"status": Status.RESOLVED}, [], None)
        for incident_id in (opened.incident_id, acknowledged.incident_id):
            table.update_item(
                Key={"pk": f"INCIDENT#{incident_id}", "sk": "META"},
                UpdateExpression="REMOVE active_pk",
            )

        assert "backfilled 2 active incidents" in ensure_table(client, table.name)
        assert {i.incident_id for i in store.list_incidents(active_only=True)} == {
            opened.incident_id,
            acknowledged.incident_id,
        }
        assert "backfilled 0 active incidents" in ensure_table(client, table.name)
