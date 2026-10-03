"""Incident storage in DynamoDB. See setup_table.py for the table layout."""

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from incident_api import config
from incident_api.models import Diagnosis, Incident, IncidentCreate, Status, TimelineEvent

LIST_LIMIT = 200
ACTIVE = "ACTIVE"


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def _conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
        reason.get("Code") == "ConditionalCheckFailed"
        for reason in exc.response.get("CancellationReasons", [])
    )


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_clock_lock = threading.Lock()
_last_us = 0


def now_iso() -> str:
    # WebSocket and REST responses can race; strictly increasing timestamps let
    # the dashboard reject an older snapshot even for rapid consecutive writes.
    return _unique_now().isoformat(timespec="microseconds")


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


def _dynamo_value(value):
    """DynamoDB rejects Python floats, including those nested in alert snapshots."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _dynamo_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_dynamo_value(inner) for inner in value]
    return value


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
        created = ("created", f"Incident created: {data.title}")
        for attempt in range(8):
            incident, actions = self.automatic_incident_actions(data)
            try:
                self._transact(
                    [*actions, *self._event_puts(incident.incident_id, [created], data.actor)]
                )
                return incident
            except ClientError as exc:
                reasons = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
                if (
                    exc.response["Error"]["Code"] != "TransactionCanceledException"
                    or reasons[:1] != ["ConditionalCheckFailed"]
                    or attempt == 7
                ):
                    raise
                time.sleep(0.025 * (attempt + 1))
        raise RuntimeError("unreachable incident creation retry state")

    def automatic_incident_actions(
        self, data: IncidentCreate, initial_fields: dict | None = None
    ) -> tuple[Incident, list[dict]]:
        """Prepare a new incident and counter increment for one atomic transaction.

        A competing writer invalidates the counter condition; callers retry with a fresh
        candidate. Failed races therefore leave no visible gaps in incident numbers.
        """
        current = self.table.get_item(
            Key={"pk": "COUNTER", "sk": "INCIDENT"}, ConsistentRead=True
        ).get("Item")
        value = int(current["value"]) if current else 0
        incident, put = self._new_incident(data, f"INC-{1001 + value}", initial_fields)
        counter = {
            "Update": {
                "TableName": self.table.name,
                "Key": {"pk": "COUNTER", "sk": "INCIDENT"},
                "UpdateExpression": "SET #value = :next",
                "ConditionExpression": "#value = :previous"
                if current
                else "attribute_not_exists(pk)",
                "ExpressionAttributeNames": {"#value": "value"},
                "ExpressionAttributeValues": {
                    ":next": value + 1,
                    **({":previous": value} if current else {}),
                },
            }
        }
        return incident, [counter, put]

    def _new_incident(
        self, data: IncidentCreate, incident_id: str, initial_fields: dict | None = None
    ) -> tuple[Incident, dict]:
        now = now_iso()
        incident = Incident.model_validate(
            {
                "incident_id": incident_id,
                "title": data.title,
                "service": data.service,
                "severity": data.severity,
                "status": Status.OPEN,
                "trigger": data.trigger,
                "summary": data.summary,
                "created_at": now,
                "updated_at": now,
                **(initial_fields or {}),
            }
        )
        item = {
            k: _dynamo_value(v)
            for k, v in incident.model_dump(mode="json").items()
            if v is not None
        }
        item |= {
            "pk": _pk(incident_id),
            "sk": "META",
            "gsi1pk": "INCIDENT",
            "gsi1sk": f"{now}#{incident_id}",
            "active_pk": ACTIVE,
        }
        return incident, {
            "Put": {
                "TableName": self.table.name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }

    @staticmethod
    def incident_key(incident_id: str) -> dict:
        return {"pk": _pk(incident_id), "sk": "META"}

    @staticmethod
    def event_time() -> str:
        return _unique_now().isoformat(timespec="microseconds")

    @staticmethod
    def alarm_marker_key(event_id: str) -> dict:
        return {"pk": f"ALARM_EVENT#{event_id}", "sk": "EVENT_MARKER"}

    def alarm_marker(self, event_id: str) -> dict | None:
        marker = self.table.get_item(Key=self.alarm_marker_key(event_id), ConsistentRead=True).get(
            "Item"
        )
        if marker:
            return marker
        # Read markers written by the first Step 6 deployment during in-flight retries.
        return self.table.get_item(
            Key={"pk": f"ALARM_EVENT#{event_id}", "sk": "META"}, ConsistentRead=True
        ).get("Item")

    def event_puts(self, incident_id: str, events: list[Event], actor: str | None) -> list:
        return self._event_puts(incident_id, events, actor)

    def transact(self, items: list[dict]) -> None:
        self._transact(items)

    def correlation_update_action(self, incident: Incident, fields: dict) -> dict:
        """Update derived fields only if the incident has not changed since read."""
        changes = {**fields, "updated_at": now_iso()}
        names = {f"#{name}": name for name in [*changes, "status"]}
        values = {f":{name}": _dynamo_value(value) for name, value in changes.items()}
        values.update({":old": incident.updated_at, ":resolved": Status.RESOLVED.value})
        return {
            "Update": {
                "TableName": self.table.name,
                "Key": self.incident_key(incident.incident_id),
                "UpdateExpression": "SET " + ", ".join(f"#{name} = :{name}" for name in changes),
                "ConditionExpression": "#status <> :resolved AND #updated_at = :old",
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }
        }

    def record_evidence(
        self, incident_id: str, event_id: str, at: str, bucket: str, prefix: str
    ) -> None:
        message = f"Evidence: s3://{bucket}/{prefix}/"
        try:
            self.table.put_item(
                Item={
                    "pk": _pk(incident_id),
                    "sk": f"EVENT#{at}#EVIDENCE#{event_id}",
                    "at": at,
                    "kind": "evidence",
                    "message": message,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def claim_diagnosis(self, incident_id: str) -> str | None:
        """Claim once, or reclaim a worker that died more than five minutes ago."""
        claim = now_iso()
        cutoff = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="milliseconds")
        diagnosis = Diagnosis(status="RUNNING", claimed_at=claim)
        update = {
            "Update": {
                "TableName": self.table.name,
                "Key": self.incident_key(incident_id),
                "UpdateExpression": "SET #diagnosis = :diagnosis, #updated = :now",
                "ConditionExpression": (
                    "attribute_exists(pk) AND #status <> :resolved AND "
                    "(attribute_not_exists(#diagnosis) OR "
                    "(#diagnosis.#ds = :running AND #diagnosis.#claimed < :cutoff))"
                ),
                "ExpressionAttributeNames": {
                    "#diagnosis": "diagnosis",
                    "#ds": "status",
                    "#claimed": "claimed_at",
                    "#status": "status",
                    "#updated": "updated_at",
                },
                "ExpressionAttributeValues": {
                    ":diagnosis": diagnosis.model_dump(mode="json"),
                    ":now": claim,
                    ":resolved": Status.RESOLVED.value,
                    ":running": "RUNNING",
                    ":cutoff": cutoff,
                },
            }
        }
        try:
            self._transact(
                [
                    update,
                    *self._event_puts(incident_id, [("diagnosis", "AI analysis started")], "ai"),
                ]
            )
        except ClientError as exc:
            if _conditional_failure(exc):
                return None
            raise
        return claim

    def finish_diagnosis(
        self, incident_id: str, claim: str, diagnosis: Diagnosis
    ) -> Incident | None:
        message = (
            "AI analysis ready for review"
            if diagnosis.status == "READY"
            else "AI analysis unavailable"
        )
        update = {
            "Update": {
                "TableName": self.table.name,
                "Key": self.incident_key(incident_id),
                "UpdateExpression": "SET #diagnosis = :diagnosis, #updated = :now",
                "ConditionExpression": "#diagnosis.#claimed = :claim AND #diagnosis.#ds = :running",
                "ExpressionAttributeNames": {
                    "#diagnosis": "diagnosis",
                    "#claimed": "claimed_at",
                    "#ds": "status",
                    "#updated": "updated_at",
                },
                "ExpressionAttributeValues": {
                    ":diagnosis": _dynamo_value(diagnosis.model_dump(mode="json")),
                    ":now": now_iso(),
                    ":claim": claim,
                    ":running": "RUNNING",
                },
            }
        }
        try:
            self._transact([update, *self._event_puts(incident_id, [("diagnosis", message)], "ai")])
        except ClientError as exc:
            if _conditional_failure(exc):
                return None  # a newer worker replaced this claim
            raise
        return self.get(incident_id)

    def change_recommendation(
        self,
        incident_id: str,
        index: int,
        action_id: str,
        from_status: str,
        to_status: str,
        actor: str,
        message: str,
        *,
        stale_before: str | None = None,
    ) -> Incident:
        """The action transition and its audit entry are a single transaction."""
        path = f"#diagnosis.#actions[{index}]"
        terminal = to_status in {"SUCCEEDED", "FAILED"}
        names = {
            "#diagnosis": "diagnosis",
            "#actions": "recommended_actions",
            "#id": "id",
            "#as": "status",
            "#actor": "decided_by",
            "#changed": "changed_at",
            "#updated": "updated_at",
            **({} if terminal else {"#status": "status"}),
        }
        update = {
            "Update": {
                "TableName": self.table.name,
                "Key": self.incident_key(incident_id),
                "UpdateExpression": (
                    f"SET {path}.#as = :next, {path}.#actor = :actor, "
                    f"{path}.#changed = :now, #updated = :now"
                ),
                "ConditionExpression": (
                    ("" if terminal else "#status <> :resolved AND ")
                    + "#diagnosis.#ds = :ready AND "
                    f"{path}.#id = :id AND {path}.#as = :previous"
                    + (f" AND {path}.#changed < :cutoff" if stale_before else "")
                ),
                "ExpressionAttributeNames": names | {"#ds": "status"},
                "ExpressionAttributeValues": {
                    ":next": to_status,
                    ":previous": from_status,
                    ":actor": actor,
                    ":now": now_iso(),
                    ":ready": "READY",
                    ":id": action_id,
                    **({} if terminal else {":resolved": Status.RESOLVED.value}),
                    **({":cutoff": stale_before} if stale_before else {}),
                },
            }
        }
        try:
            self._transact(
                [update, *self._event_puts(incident_id, [("remediation", message)], actor)]
            )
        except ClientError as exc:
            if _conditional_failure(exc):
                if self.get(incident_id) is None:
                    raise NotFound(incident_id) from exc
                raise Conflict(f"recommendation {action_id} is no longer {from_status}") from exc
            raise
        incident = self.get(incident_id)
        assert incident is not None
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
