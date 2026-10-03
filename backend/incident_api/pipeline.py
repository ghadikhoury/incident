"""Turn CloudWatch alarm transitions into durable incidents and evidence."""

import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import ClientError

from incident_api import config
from incident_api.models import Incident, Severity, Status
from incident_api.store import IncidentStore, _pk, _unique_now, now_iso

LOG = logging.getLogger(__name__)
WINDOW_SECONDS = 600
SERVICES = {service.name for service in config.SERVICES}
SIGNALS = {"health", "errors", "latency"}


def _transition(event: dict) -> tuple[str, str, str, datetime]:
    if (
        event.get("source") != "aws.cloudwatch"
        or event.get("detail-type") != "CloudWatch Alarm State Change"
    ):
        raise ValueError("not a CloudWatch alarm state change")
    detail = event["detail"]
    name = detail["alarmName"]
    parts = name.split("-")
    if (
        len(parts) != 3
        or parts[0] != "incident"
        or parts[1] not in SERVICES
        or parts[2] not in SIGNALS
    ):
        raise ValueError(f"unmanaged alarm: {name}")
    state = detail["state"]["value"]
    if state not in {"ALARM", "OK", "INSUFFICIENT_DATA"}:
        raise ValueError(f"unknown alarm state: {state}")
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
        marker_key = {"pk": f"ALARM_EVENT#{event_id}", "sk": "META"}
        marker = self.store.table.get_item(Key=marker_key, ConsistentRead=True).get("Item")
        if marker:
            incident_id = marker.get("incident_id")
        else:
            incident_id = self._record(event_id, name, service, state, when)
        if incident_id is None:
            return None
        if state == "ALARM" or state == "INSUFFICIENT_DATA":
            self._capture(incident_id, event_id, name, service, when, event)
        self.sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps({"incident_id": incident_id, "event_id": event_id}),
        )
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
            marker = table.get_item(
                Key={"pk": f"ALARM_EVENT#{event_id}", "sk": "META"}, ConsistentRead=True
            ).get("Item")
            if marker:
                return marker.get("incident_id")
            alarm = table.get_item(
                Key={"pk": f"ALARM#{name}", "sk": "CURRENT"}, ConsistentRead=True
            ).get("Item")
            if alarm and alarm["state_at"] > ts:
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
                incident_id = self.store._next_id() if create else active.incident_id
            marker_item = {
                "pk": f"ALARM_EVENT#{event_id}",
                "sk": "META",
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
                    incident = Incident(
                        incident_id=incident_id,
                        title=f"{service} {name.rsplit('-', 1)[-1]} alarm"
                        if state == "ALARM"
                        else f"Monitoring degraded: {service}",
                        service=service,
                        severity=Severity.SEV3,
                        status=Status.OPEN,
                        trigger="CLOUDWATCH",
                        summary=f"{name} entered {state}",
                        created_at=now_iso(),
                        updated_at=now_iso(),
                    )
                    item = {
                        k: v for k, v in incident.model_dump(mode="json").items() if v is not None
                    }
                    item |= {
                        "pk": _pk(incident_id),
                        "sk": "META",
                        "gsi1pk": "INCIDENT",
                        "gsi1sk": f"{incident.created_at}#{incident_id}",
                        "active_pk": "ACTIVE",
                    }
                    transaction.append(
                        {
                            "Put": {
                                "TableName": table.name,
                                "Item": item,
                                "ConditionExpression": "attribute_not_exists(pk)",
                            }
                        }
                    )
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
                            "ConditionCheck": {
                                "TableName": table.name,
                                "Key": {"pk": "PIPELINE", "sk": "ACTIVE"},
                                "ConditionExpression": "incident_id = :id AND expires_at > :now",
                                "ExpressionAttributeValues": {":id": incident_id, ":now": now},
                            }
                        }
                    )
                    transaction.append(
                        {
                            "ConditionCheck": {
                                "TableName": table.name,
                                "Key": {"pk": _pk(incident_id), "sk": "META"},
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
                        "ConditionExpression": "attribute_not_exists(state_at) OR state_at <= :at",
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
                    self.store._event_puts(incident_id, [("alarm", message)], "cloudwatch")
                )
            marker_item["evidence_at"] = _unique_now().isoformat(timespec="microseconds")
            try:
                self.store._transact(transaction)
                return incident_id
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
                if attempt == 7:
                    raise
                time.sleep(0.025 * (attempt + 1))
        return None

    def _capture(
        self, incident_id: str, event_id: str, name: str, service: str, when: datetime, event: dict
    ) -> None:
        prefix = f"incidents/{incident_id}/events/{event_id}"
        start, end = when - timedelta(minutes=5), when + timedelta(minutes=5)
        groups = [f"/incident/{service}", "/incident/backend"]
        query = "fields @timestamp, @log, @message | sort @timestamp desc | limit 200"
        result = self.logs.start_query(
            logGroupNames=groups,
            startTime=int(start.timestamp()),
            endTime=int(end.timestamp()),
            queryString=query,
        )
        query_id = result["queryId"]
        logs = None
        for _ in range(24):
            logs = self.logs.get_query_results(queryId=query_id)
            if logs["status"] not in {"Running", "Scheduled"}:
                break
            time.sleep(0.5)
        if logs is None or logs["status"] not in {"Complete", "PartialSuccess"}:
            raise RuntimeError(f"Logs Insights query {query_id} did not complete: {logs}")
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
        alarm = self.cloudwatch.describe_alarms(AlarmNames=[name])["MetricAlarms"]
        for filename, payload in (
            ("event.json", {"event": event, "alarm": alarm}),
            ("logs.json", logs),
            ("metrics.json", metrics),
        ):
            self.s3.put_object(
                Bucket=self.bucket,
                Key=f"{prefix}/{filename}",
                Body=json.dumps(payload, default=str).encode(),
                ContentType="application/json",
                ServerSideEncryption="AES256",
            )
        # A deterministic key makes repeated Lambda delivery safe after an S3 or SQS error.
        marker = self.store.table.get_item(
            Key={"pk": f"ALARM_EVENT#{event_id}", "sk": "META"}, ConsistentRead=True
        )["Item"]
        try:
            self.store.table.put_item(
                Item={
                    "pk": _pk(incident_id),
                    "sk": f"EVENT#{marker['evidence_at']}#EVIDENCE#{event_id}",
                    "at": marker["evidence_at"],
                    "kind": "evidence",
                    "message": f"Evidence: s3://{self.bucket}/{prefix}/",
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise


def lambda_handler(event: dict, context):
    try:
        incident_id = AlarmPipeline.from_environment().process(event)
    except ValueError:
        LOG.info("Ignoring unrelated alarm event", exc_info=True)
        return {"ignored": True}
    return {"incident_id": incident_id}
