"""Incident storage in DynamoDB. See setup_table.py for the table layout."""

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from incident_api import config
from incident_api.models import Incident, IncidentCreate, Status, TimelineEvent

LIST_LIMIT = 200


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_clock_lock = threading.Lock()
_last_us = 0


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _unique_now() -> datetime:
    """Current time, but strictly increasing within this process.

    The system clock can return the same value twice (on Windows it ticks coarsely),
    which would make the order of quickly-written timeline events random.
    """
    global _last_us
    with _clock_lock:
        _last_us = max(time.time_ns() // 1000, _last_us + 1)
        return _EPOCH + timedelta(microseconds=_last_us)


def _pk(incident_id: str) -> str:
    return f"INCIDENT#{incident_id}"


def _to_incident(item: dict) -> Incident:
    return Incident.model_validate(item)


class IncidentStore:
    def __init__(self, table):
        self.table = table

    @classmethod
    def from_config(cls) -> "IncidentStore":
        session = boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.AWS_REGION)
        return cls(session.resource("dynamodb").Table(config.TABLE_NAME))

    def create(self, data: IncidentCreate) -> Incident:
        now = now_iso()
        incident = Incident(
            incident_id=self._next_id(),
            title=data.title,
            service=data.service,
            severity=data.severity,
            status=Status.OPEN,
            trigger=data.trigger,
            summary=data.summary,
            created_at=now,
            updated_at=now,
        )
        item = {k: v for k, v in incident.model_dump(mode="json").items() if v is not None}
        item |= {"pk": _pk(incident.incident_id), "sk": "META", "gsi1pk": "INCIDENT"}
        item["gsi1sk"] = f"{now}#{incident.incident_id}"  # unique even if created_at ties
        self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        self.add_event(
            incident.incident_id, "created", f"Incident created: {data.title}", data.actor
        )
        return incident

    def get(self, incident_id: str) -> Incident | None:
        item = self.table.get_item(Key={"pk": _pk(incident_id), "sk": "META"}).get("Item")
        return _to_incident(item) if item else None

    def list_incidents(self, active_only: bool = False) -> list[Incident]:
        """Newest first. Only the most recent LIST_LIMIT incidents are considered."""
        response = self.table.query(
            IndexName="by-created",
            KeyConditionExpression=Key("gsi1pk").eq("INCIDENT"),
            ScanIndexForward=False,
            Limit=LIST_LIMIT,
        )
        incidents = [_to_incident(item) for item in response["Items"]]
        if active_only:
            incidents = [i for i in incidents if i.status != Status.RESOLVED]
        return incidents

    def update(
        self, incident_id: str, changes: dict, *, require_status: Status | None = None
    ) -> Incident:
        """Apply field changes (a value of None removes the field).

        Resolved incidents can't be changed. If require_status is given, the incident must
        currently be in that status. Raises NotFound or Conflict.
        """
        now = now_iso()
        sets = {"updated_at": now}
        removes = []
        for field, value in changes.items():
            if value is None:
                removes.append(field)
            else:
                sets[field] = str(value)
        if sets.get("status") == Status.RESOLVED:
            sets["resolved_at"] = now

        names = {f"#{f}": f for f in [*sets, *removes, "status"]}
        values = {f":{f}": v for f, v in sets.items()} | {":resolved": Status.RESOLVED.value}
        expression = "SET " + ", ".join(f"#{f} = :{f}" for f in sets)
        if removes:
            expression += " REMOVE " + ", ".join(f"#{f}" for f in removes)
        condition = "attribute_exists(pk) AND #status <> :resolved"
        if require_status:
            condition += " AND #status = :required"
            values[":required"] = require_status.value

        try:
            response = self.table.update_item(
                Key={"pk": _pk(incident_id), "sk": "META"},
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            current = self.get(incident_id)
            if current is None:
                raise NotFound(incident_id) from exc
            raise Conflict(f"{incident_id} is {current.status}") from exc
        return _to_incident(response["Attributes"])

    def add_event(self, incident_id: str, kind: str, message: str, actor: str | None) -> None:
        now = _unique_now()
        at = now.isoformat(timespec="milliseconds")
        sort_time = now.isoformat(timespec="microseconds")  # keeps quick successive events in order
        item = {
            "pk": _pk(incident_id),
            "sk": f"EVENT#{sort_time}#{uuid.uuid4().hex[:8]}",
            "at": at,
            "kind": kind,
            "message": message,
        }
        if actor:
            item["actor"] = actor
        self.table.put_item(Item=item)

    def timeline(self, incident_id: str) -> list[TimelineEvent]:
        response = self.table.query(
            KeyConditionExpression=Key("pk").eq(_pk(incident_id)) & Key("sk").begins_with("EVENT#")
        )
        return [TimelineEvent.model_validate(item) for item in response["Items"]]

    def _next_id(self) -> str:
        response = self.table.update_item(
            Key={"pk": "COUNTER", "sk": "INCIDENT"},
            UpdateExpression="ADD #value :one",
            ExpressionAttributeNames={"#value": "value"},
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return f"INC-{1000 + int(response['Attributes']['value'])}"
