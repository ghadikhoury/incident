"""Schema upgrades must preserve the active list for incidents created earlier."""

import boto3
from moto import mock_aws

from incident_api.models import IncidentCreate, Status
from incident_api.setup_table import ATTRIBUTES, INDEXES, ensure_table
from incident_api.store import IncidentStore


def test_existing_table_index_upgrade_backfills_every_unresolved_incident(monkeypatch):
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
        unresolved = []
        for status in Status:
            incident = store.create(IncidentCreate(title=f"old {status}", service="payment"))
            if status != Status.OPEN:
                store.update(incident.incident_id, {"status": status}, [], None)
            if status != Status.RESOLVED:
                unresolved.append(incident.incident_id)
                # Simulate a row written before the active index existed.
                table.update_item(
                    Key={"pk": f"INCIDENT#{incident.incident_id}", "sk": "META"},
                    UpdateExpression="REMOVE active_pk",
                )

        assert f"backfilled {len(unresolved)} active incidents" in ensure_table(client, table.name)
        assert {i.incident_id for i in store.list_incidents(active_only=True)} == set(unresolved)
        assert "backfilled 0 active incidents" in ensure_table(client, table.name)
