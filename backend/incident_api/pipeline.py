"""Turn CloudWatch alarm transitions into durable incidents and evidence."""

import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import ClientError

from incident_api import config
from incident_api.models import IncidentCreate, Status
from incident_api.store import IncidentStore

LOG = logging.getLogger(__name__)
WINDOW_SECONDS = 600
BACKFILL_DELAY_SECONDS = 360
SERVICES = {service.name for service in config.SERVICES}
SIGNALS = {"health", "errors", "latency"}
EXCLUDE_INJECTION = (
    "filter (not ispresent(message) or message not like /^chaos/) "
    'and (not ispresent(endpoint) or endpoint != "/chaos") '
    "and @message not like /\\/api\\/simulation\\/(failure|recover)/"
)
SUMMARY_QUERY = (
    "fields service, endpoint, status_code, error_type, message | "
    + EXCLUDE_INJECTION
    + " | stats count(*) as events by bin(1m) as minute, service, endpoint, "
    "status_code, error_type | sort minute asc"
)
SAMPLES_QUERY = (
    "fields @timestamp, service, level, endpoint, status_code, error_type, "
    "error_message, message, trace_id | "
    + EXCLUDE_INJECTION
    + ' | filter level in ["ERROR", "WARNING"] or status_code >= 500 or ispresent(error_type) '
    "| sort @timestamp asc | limit 200"
)


class IgnoredAlarm(Exception):
    """An EventBridge event outside this demo's managed alarm set."""


def _transition(event: dict) -> tuple[str, str, str, datetime]:
    if (
        event.get("source") != "aws.cloudwatch"
        or event.get("detail-type") != "CloudWatch Alarm State Change"
    ):
        raise IgnoredAlarm("not a CloudWatch alarm state change")
    detail = event["detail"]
    name = detail["alarmName"]
    parts = name.split("-")
    if (
        len(parts) != 3
        or parts[0] != "incident"
        or parts[1] not in SERVICES
        or parts[2] not in SIGNALS
    ):
        raise IgnoredAlarm(f"unmanaged alarm: {name}")
    state = detail["state"]["value"]
    if state not in {"ALARM", "OK", "INSUFFICIENT_DATA"}:
        raise IgnoredAlarm(f"unknown alarm state: {state}")
    when = datetime.fromisoformat(detail["state"]["timestamp"].replace("Z", "+00:00"))
    if when.utcoffset() is None:
        raise ValueError("alarm timestamp must have a timezone")
    if not event.get("id"):
        raise ValueError("event id is required")
    return name, parts[1], state, when.astimezone(UTC)


class AlarmPipeline:
    def __init__(
        self,
        store: IncidentStore,
        logs,
        cloudwatch,
        s3,
        sqs,
        ec2,
        *,
        bucket: str,
        queue_url: str,
        backfill_queue_url: str,
        instance_id: str,
    ):
        self.store = store
        self.logs = logs
        self.cloudwatch = cloudwatch
        self.s3 = s3
        self.sqs = sqs
        self.ec2 = ec2
        self.bucket = bucket
        self.queue_url = queue_url
        self.backfill_queue_url = backfill_queue_url
        self.instance_id = instance_id

    @classmethod
    def from_environment(cls):
        session = boto3.Session(region_name=config.AWS_REGION)
        store = IncidentStore(session.resource("dynamodb").Table(config.TABLE_NAME))
        return cls(
            store,
            session.client("logs"),
            session.client("cloudwatch"),
            session.client("s3"),
            session.client("sqs"),
            session.client("ec2"),
            bucket=os.environ["INCIDENT_EVIDENCE_BUCKET"],
            queue_url=os.environ["INCIDENT_QUEUE_URL"],
            backfill_queue_url=os.environ["INCIDENT_BACKFILL_QUEUE_URL"],
            instance_id=os.environ["INCIDENT_INSTANCE_ID"],
        )

    def process(self, event: dict) -> str | None:
        name, service, state, when = _transition(event)
        if state == "INSUFFICIENT_DATA":
            if name.endswith("-health") and not self._instance_running():
                LOG.info("Ignoring missing health stream while instance is stopped: %s", name)
                return None
            if not name.endswith("-health"):
                return None  # request metrics legitimately stop with no traffic
        event_id = event["id"]
        marker = self.store.alarm_marker(event_id)
        if marker:
            incident_id = marker.get("incident_id")
        else:
            incident_id = self._record(event_id, name, service, state, when)
        if incident_id is None:
            return None
        self._notify(incident_id, event_id)
        if state in {"ALARM", "INSUFFICIENT_DATA"}:
            self._capture(incident_id, event_id, name, service, when, event, complete=False)
            self._notify(incident_id, event_id)
            self.sqs.send_message(
                QueueUrl=self.backfill_queue_url,
                DelaySeconds=BACKFILL_DELAY_SECONDS,
                MessageBody=json.dumps({"incident_id": incident_id, "event": event}),
            )
        return incident_id

    def _notify(self, incident_id: str, event_id: str) -> None:
        self.sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps({"incident_id": incident_id, "event_id": event_id}),
        )

    def backfill(self, body: dict) -> str:
        event = body["event"]
        name, service, state, when = _transition(event)
        if state not in {"ALARM", "INSUFFICIENT_DATA"}:
            raise ValueError("backfill is only valid for evidence-bearing transitions")
        marker = self.store.alarm_marker(event["id"])
        if not marker or marker.get("incident_id") != body["incident_id"]:
            raise ValueError("backfill has no matching recorded alarm event")
        incident_id = body["incident_id"]
        self._capture(incident_id, event["id"], name, service, when, event, complete=True)
        self._notify(incident_id, event["id"])
        return incident_id

    def _instance_running(self) -> bool:
        instances = self.ec2.describe_instances(InstanceIds=[self.instance_id])["Reservations"]
        return any(
            instance["State"]["Name"] == "running"
            for reservation in instances
            for instance in reservation["Instances"]
        )

    def _record(
        self, event_id: str, name: str, service: str, state: str, when: datetime
    ) -> str | None:
        table = self.store.table
        ts = when.isoformat(timespec="milliseconds")
        is_problem = state != "OK"
        for attempt in range(8):
            marker = self.store.alarm_marker(event_id)
            if marker:
                return marker.get("incident_id")
            alarm = table.get_item(
                Key={"pk": f"ALARM#{name}", "sk": "CURRENT"}, ConsistentRead=True
            ).get("Item")
            if alarm and alarm["state_at"] >= ts:
                return None  # stale transition; newer state already recorded
            if state == "OK":
                incident_id = alarm.get("incident_id") if alarm else None
                create = False
            else:
                lock = table.get_item(
                    Key={"pk": "PIPELINE", "sk": "ACTIVE"}, ConsistentRead=True
                ).get("Item")
                now = int(time.time())
                active = (
                    self.store.get(lock["incident_id"])
                    if lock and lock["expires_at"] > now
                    else None
                )
                create = active is None or active.status == Status.RESOLVED
                if create:
                    title = (
                        f"{service} {name.rsplit('-', 1)[-1]} alarm"
                        if state == "ALARM"
                        else f"Monitoring degraded: {service}"
                    )
                    incident, create_actions = self.store.automatic_incident_actions(
                        IncidentCreate(
                            title=title,
                            service=service,
                            trigger="CLOUDWATCH",
                            summary=f"{name} entered {state}",
                        )
                    )
                    incident_id = incident.incident_id
                else:
                    incident_id = active.incident_id
            marker_item = {
                **self.store.alarm_marker_key(event_id),
                "incident_id": incident_id,
            }
            transaction = [
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": marker_item,
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
            ]
            if is_problem:
                if create:
                    transaction.extend(create_actions)
                    lock_condition = "attribute_not_exists(pk) OR expires_at <= :now"
                    lock_values = {":now": now}
                    if lock:
                        lock_condition += " OR incident_id = :previous"
                        lock_values[":previous"] = lock["incident_id"]
                    transaction.append(
                        {
                            "Put": {
                                "TableName": table.name,
                                "Item": {
                                    "pk": "PIPELINE",
                                    "sk": "ACTIVE",
                                    "incident_id": incident_id,
                                    "expires_at": now + WINDOW_SECONDS,
                                },
                                "ConditionExpression": lock_condition,
                                "ExpressionAttributeValues": lock_values,
                            }
                        }
                    )
                else:
                    transaction.append(
                        {
                            "Update": {
                                "TableName": table.name,
                                "Key": {"pk": "PIPELINE", "sk": "ACTIVE"},
                                "UpdateExpression": "SET expires_at = :next",
                                "ConditionExpression": "incident_id = :id AND expires_at > :now",
                                "ExpressionAttributeValues": {
                                    ":id": incident_id,
                                    ":now": now,
                                    ":next": now + WINDOW_SECONDS,
                                },
                            }
                        }
                    )
                    transaction.append(
                        {
                            "ConditionCheck": {
                                "TableName": table.name,
                                "Key": self.store.incident_key(incident_id),
                                "ConditionExpression": "#status <> :resolved",
                                "ExpressionAttributeNames": {"#status": "status"},
                                "ExpressionAttributeValues": {":resolved": Status.RESOLVED.value},
                            }
                        }
                    )
            transaction.append(
                {
                    "Update": {
                        "TableName": table.name,
                        "Key": {"pk": f"ALARM#{name}", "sk": "CURRENT"},
                        "UpdateExpression": (
                            "SET state_at = :at, #state = :state, incident_id = :id"
                        ),
                        "ConditionExpression": "attribute_not_exists(state_at) OR state_at < :at",
                        "ExpressionAttributeNames": {"#state": "state"},
                        "ExpressionAttributeValues": {
                            ":at": ts,
                            ":state": state,
                            ":id": incident_id,
                        },
                    }
                }
            )
            if incident_id:
                message = f"{name} entered {state} at {ts}"
                transaction.extend(
                    self.store.event_puts(incident_id, [("alarm", message)], "cloudwatch")
                )
            marker_item["evidence_at"] = self.store.event_time()
            try:
                self.store.transact(transaction)
                return incident_id
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
                if attempt == 7:
                    raise
                time.sleep(0.025 * (attempt + 1))
        return None

    def _capture(
        self,
        incident_id: str,
        event_id: str,
        name: str,
        service: str,
        when: datetime,
        event: dict,
        *,
        complete: bool,
    ) -> None:
        prefix = f"incidents/{incident_id}/events/{event_id}"
        start, requested_end = when - timedelta(minutes=5), when + timedelta(minutes=5)
        end = requested_end if complete else min(requested_end, datetime.now(UTC))
        groups = [f"/incident/{service}", "/incident/backend"]
        summary = self._query_logs(groups, start, end, SUMMARY_QUERY)
        samples = self._query_logs(groups, start, end, SAMPLES_QUERY)
        window = {
            "start": start.isoformat(),
            "end": requested_end.isoformat(),
            "captured_through": end.isoformat(),
            "complete": complete,
        }
        logs = {"window": window, "minute_summary": summary, "error_samples": samples}
        metric_queries = [
            {
                "Id": metric.lower(),
                "MetricStat": {
                    "Metric": {
                        "Namespace": "Incident",
                        "MetricName": metric,
                        "Dimensions": [{"Name": "Service", "Value": service}],
                    },
                    "Period": 60,
                    "Stat": stat,
                },
            }
            for metric, stat in (
                ("Requests", "Sum"),
                ("Errors", "Sum"),
                ("Latency", "p90"),
                ("HealthCheckFailed", "Average"),
            )
        ]
        metrics = self.cloudwatch.get_metric_data(
            MetricDataQueries=metric_queries,
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampAscending",
        )
        metrics["window"] = window
        objects = [("logs.json", logs), ("metrics.json", metrics)]
        if not complete:
            alarm = self.cloudwatch.describe_alarms(AlarmNames=[name])["MetricAlarms"]
            objects.append(("event.json", {"event": event, "alarm": alarm}))
        for filename, payload in objects:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=f"{prefix}/{filename}",
                Body=json.dumps(payload, default=str).encode(),
                ContentType="application/json",
                ServerSideEncryption="AES256",
            )
        marker = self.store.alarm_marker(event_id)
        if not marker:
            raise RuntimeError(f"missing alarm marker for {event_id}")
        if not complete:
            self.store.record_evidence(
                incident_id, event_id, marker["evidence_at"], self.bucket, prefix
            )

    def _query_logs(
        self, groups: list[str], start: datetime, end: datetime, query: str
    ) -> list[dict]:
        result = self.logs.start_query(
            logGroupNames=groups,
            startTime=int(start.timestamp()),
            endTime=int(end.timestamp()),
            queryString=query,
        )
        query_id = result["queryId"]
        logs = None
        for _ in range(16):
            logs = self.logs.get_query_results(queryId=query_id)
            if logs["status"] not in {"Running", "Scheduled"}:
                break
            time.sleep(0.5)
        if logs is None or logs["status"] not in {"Complete", "PartialSuccess"}:
            raise RuntimeError(f"Logs Insights query {query_id} did not complete: {logs}")
        return [
            {field["field"]: field["value"] for field in row if field["field"] != "@ptr"}
            for row in logs["results"]
        ]


def lambda_handler(event: dict, context):
    pipeline = AlarmPipeline.from_environment()
    if "Records" in event:
        for record in event["Records"]:
            if record.get("eventSource") != "aws:sqs":
                raise ValueError("unexpected Lambda record source")
            pipeline.backfill(json.loads(record["body"]))
        return {"backfilled": len(event["Records"])}
    try:
        incident_id = pipeline.process(event)
    except IgnoredAlarm:
        LOG.info("Ignoring unrelated alarm event", exc_info=True)
        return {"ignored": True}
    return {"incident_id": incident_id}
