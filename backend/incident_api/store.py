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
ACTIVE = "ACTIVE"


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


Event = tuple[str, str]  # (kind, message) of a timeline entry


class IncidentStore:
    """Every change to an incident is written in one DynamoDB transaction together with
    its timeline events, so the incident and its audit trail can never disagree."""

    def __init__(self, table):
        self.table = table
        self.client = table.meta.client  # converts plain Python values like the table does

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
        item |= {
            "pk": _pk(incident.incident_id),
            "sk": "META",
            "gsi1pk": "INCIDENT",
            "gsi1sk": f"{now}#{incident.incident_id}",  # unique even if created_at ties
            "active_pk": ACTIVE,
        }
        put = {"Put": {"TableName": self.table.name, "Item": item}}
        put["Put"]["ConditionExpression"] = "attribute_not_exists(pk)"
        created = ("created", f"Incident created: {data.title}")
        self._transact([put, *self._event_puts(incident.incident_id, [created], data.actor)])
        return incident

    def get(self, incident_id: str) -> Incident | None:
        item = self.table.get_item(
            Key={"pk": _pk(incident_id), "sk": "META"}, ConsistentRead=True
        ).get("Item")
        return _to_incident(item) if item else None

    def list_incidents(self, active_only: bool = False) -> list[Incident]:
        """Newest first. Active incidents are always complete (read from the sparse
        active index); the full list is capped at the newest LIST_LIMIT incidents."""
        if active_only:
            items = self._query_all(
                IndexName="active-by-created",
                KeyConditionExpression=Key("active_pk").eq(ACTIVE),
                ScanIndexForward=False,
            )
        else:
            items = self.table.query(
                IndexName="by-created",
                KeyConditionExpression=Key("gsi1pk").eq("INCIDENT"),
                ScanIndexForward=False,
                Limit=LIST_LIMIT,
            )["Items"]
        return [_to_incident(item) for item in items]

    def update(
        self,
        incident_id: str,
        changes: dict,
        events: list[Event],
        actor: str | None,
        *,
        require_status: Status | None = None,
    ) -> Incident:
        """Apply field changes (None removes a field) and record events, atomically.

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
            removes.append("active_pk")  # drops out of the active index

        names = {f"#{f}": f for f in [*sets, *removes, "status"]}
        values = {f":{f}": v for f, v in sets.items()} | {":resolved": Status.RESOLVED.value}
        expression = "SET " + ", ".join(f"#{f} = :{f}" for f in sets)
        if removes:
            expression += " REMOVE " + ", ".join(f"#{f}" for f in removes)
        condition = "attribute_exists(pk) AND #status <> :resolved"
        if require_status:
            condition += " AND #status = :required"
            values[":required"] = require_status.value

        update = {
            "Update": {
                "TableName": self.table.name,
                "Key": {"pk": _pk(incident_id), "sk": "META"},
                "UpdateExpression": expression,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }
        }
        try:
            self._transact([update, *self._event_puts(incident_id, events, actor)])
        except ClientError as exc:
            reasons = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
            if reasons[:1] == ["ConditionalCheckFailed"]:
                current = self.get(incident_id)
                if current is None:
                    raise NotFound(incident_id) from exc
                raise Conflict(f"{incident_id} is {current.status}") from exc
            if "TransactionConflict" in reasons:
                raise Conflict(f"{incident_id} is being changed by someone else; retry") from exc
            raise
        incident = self.get(incident_id)
        assert incident is not None  # it existed a moment ago and incidents are never deleted
        return incident

    def timeline(self, incident_id: str) -> list[TimelineEvent]:
        response = self.table.query(
            KeyConditionExpression=Key("pk").eq(_pk(incident_id)) & Key("sk").begins_with("EVENT#")
        )
        return [TimelineEvent.model_validate(item) for item in response["Items"]]

    def _event_puts(self, incident_id: str, events: list[Event], actor: str | None) -> list:
        puts = []
        for kind, message in events:
            now = _unique_now()
            sort_time = now.isoformat(timespec="microseconds")  # keeps quick events in order
            item = {
                "pk": _pk(incident_id),
                "sk": f"EVENT#{sort_time}#{uuid.uuid4().hex[:8]}",
                "at": now.isoformat(timespec="milliseconds"),
                "kind": kind,
                "message": message,
            }
            if actor:
                item["actor"] = actor
            puts.append({"Put": {"TableName": self.table.name, "Item": item}})
        return puts

    def _transact(self, items: list[dict]) -> None:
        self.client.transact_write_items(TransactItems=items)

    def _query_all(self, **query) -> list[dict]:
        items, start_key = [], None
        while True:
            page = self.table.query(
                **query, **({"ExclusiveStartKey": start_key} if start_key else {})
            )
            items.extend(page["Items"])
            start_key = page.get("LastEvaluatedKey")
            if not start_key:
                return items

    def _next_id(self) -> str:
        response = self.table.update_item(
            Key={"pk": "COUNTER", "sk": "INCIDENT"},
            UpdateExpression="ADD #value :one",
            ExpressionAttributeNames={"#value": "value"},
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return f"INC-{1000 + int(response['Attributes']['value'])}"
