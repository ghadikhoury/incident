"""The alarm pipeline's durable behavior, using an in-memory DynamoDB table."""

import json
import time
from datetime import UTC, datetime

import pytest

from incident_api.models import IncidentCreate, Status
from incident_api.pipeline import SAMPLES_QUERY, SUMMARY_QUERY, AlarmPipeline


class Logs:
    def __init__(self):
        self.queries = []

    def start_query(self, **kwargs):
        self.queries.append(kwargs)
        return {"queryId": "query-1"}

    def get_query_results(self, **kwargs):
        return {"status": "Complete", "results": []}


class CloudWatch:
    def get_metric_data(self, **kwargs):
        return {"MetricDataResults": []}

    def describe_alarms(self, **kwargs):
        return {"MetricAlarms": []}


class S3:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = json.loads(kwargs["Body"])


class SQS:
    def __init__(self):
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(
            {
                "queue": kwargs["QueueUrl"],
                "body": json.loads(kwargs["MessageBody"]),
                "delay": kwargs.get("DelaySeconds", 0),
            }
        )

    def updates(self):
        return [message["body"] for message in self.messages if message["queue"] == "queue"]

    def backfills(self):
        return [message for message in self.messages if message["queue"] == "backfill"]


class EC2:
    state = "running"

    def describe_instances(self, **kwargs):
        return {"Reservations": [{"Instances": [{"State": {"Name": self.state}}]}]}


def event(
    event_id, service="payment", signal="latency", state="ALARM", second=0, minute=0, value=None
):
    details = {
        "value": state,
        "timestamp": datetime(2026, 10, 2, 12, minute, second, tzinfo=UTC).isoformat(),
    }
    if value is not None:
        details["reasonData"] = json.dumps(
            {
                "threshold": {"latency": 2000, "errors": 20, "health": 0.5}[signal],
                "evaluatedDatapoints": [{"value": value}],
            }
        )
    return {
        "id": event_id,
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": f"incident-{service}-{signal}",
            "state": details,
        },
    }


def pipeline(store):
    return AlarmPipeline(
        store,
        Logs(),
        CloudWatch(),
        S3(),
        SQS(),
        EC2(),
        bucket="evidence",
        queue_url="queue",
        backfill_queue_url="backfill",
        instance_id="instance",
    )


def test_alarm_creates_evidence_timeline_and_notification(store):
    worker = pipeline(store)
    incident_id = worker.process(event("first"))
    incident = store.get(incident_id)
    assert incident.trigger == "CLOUDWATCH"
    assert incident.service == "payment"
    assert len(worker.s3.objects) == 3
    assert all(
        key.startswith(f"incidents/{incident_id}/events/first/") for key in worker.s3.objects
    )
    assert [entry.kind for entry in store.timeline(incident_id)] == ["alarm", "evidence"]
    assert worker.sqs.updates() == [
        {"incident_id": incident_id, "event_id": "first"},
        {"incident_id": incident_id, "event_id": "first"},
    ]
    assert worker.sqs.backfills()[0]["delay"] == 360


def test_five_nearby_alarms_share_one_incident(store):
    worker = pipeline(store)
    events = [
        event(str(i), service, signal, second=i)
        for i, (service, signal) in enumerate(
            [
                ("payment", "latency"),
                ("order", "errors"),
                ("gateway", "health"),
                ("order", "latency"),
                ("payment", "health"),
            ]
        )
    ]
    ids = [worker.process(alarm) for alarm in events]
    assert len(set(ids)) == 1
    assert len(store.list_incidents()) == 1
    assert len([entry for entry in store.timeline(ids[0]) if entry.kind == "alarm"]) == 5


def test_cascade_names_payment_as_probable_root_and_tracks_alerts(store):
    worker = pipeline(store)
    first = worker.process(event("order-first", "order", "errors", value=65, second=0))
    assert worker.process(event("payment", "payment", "latency", value=4900, second=2)) == first
    assert worker.process(event("gateway", "gateway", "errors", value=100, second=4)) == first
    incident = store.get(first)
    assert len(store.list_incidents()) == 1
    assert incident.probable_root == "payment"
    assert incident.service == "payment"
    assert incident.downstream_services == ["gateway", "order"]
    assert incident.correlation_label == "PROBABLE CASCADING FAILURE"
    assert incident.severity == "SEV-1"
    assert {alert.alarm_name for alert in incident.alerts} == {
        "incident-order-errors",
        "incident-payment-latency",
        "incident-gateway-errors",
    }


def test_sibling_failures_remain_distinct_even_when_upstream_alarm_joins_one(store):
    worker = pipeline(store)
    payment = worker.process(event("payment", "payment", second=0))
    inventory = worker.process(event("inventory", "inventory", second=1))
    assert payment != inventory
    order = worker.process(event("order", "order", "errors", second=2))
    assert order == inventory  # closest compatible alarm; does not merge the siblings
    assert {alert.service for alert in store.get(payment).alerts} == {"payment"}
    assert {alert.service for alert in store.get(inventory).alerts} == {"inventory", "order"}


def test_related_alarm_outside_ten_minute_window_starts_new_incident(store):
    worker = pipeline(store)
    first = worker.process(event("payment", "payment"))
    second = worker.process(event("order", "order", "errors", minute=11))
    assert second != first


def test_step_six_active_incident_keeps_its_first_service_on_rollout(store):
    old = store.create(
        IncidentCreate(
            title="payment latency alarm",
            service="payment",
            trigger="CLOUDWATCH",
            summary="incident-payment-latency entered ALARM",
        )
    )
    store.table.put_item(
        Item={
            "pk": "PIPELINE",
            "sk": "ACTIVE",
            "incident_id": old.incident_id,
            "expires_at": int(time.time()) + 600,
        }
    )
    worker = pipeline(store)
    alarm = event("order", "order", "errors")
    alarm["detail"]["state"]["timestamp"] = old.created_at
    assert worker.process(alarm) == old.incident_id
    upgraded = store.get(old.incident_id)
    assert upgraded.probable_root == "payment"
    assert {alert.service for alert in upgraded.alerts} == {"payment", "order"}


def test_recovery_updates_alert_state_and_severity_without_resolving(store):
    worker = pipeline(store)
    incident_id = worker.process(event("problem", value=5000))
    assert store.get(incident_id).severity == "SEV-2"
    worker.process(event("recovered", state="OK", minute=1, value=20))
    incident = store.get(incident_id)
    assert incident.status == Status.OPEN
    assert incident.alerts[0].state == "OK"
    assert incident.severity == "SEV-4"


def test_grouping_window_extends_when_new_alarms_arrive(store, monkeypatch):
    clock = [1000]
    monkeypatch.setattr("incident_api.pipeline.time.time", lambda: clock[0])
    worker = pipeline(store)
    first = worker.process(event("first"))
    clock[0] += 540
    assert worker.process(event("second", "order", "errors", second=1)) == first
    clock[0] += 120  # eleven minutes after first alarm, two after the second
    assert worker.process(event("third", "gateway", "health", second=2)) == first


def test_competing_create_wins_between_read_and_conditional_write(store):
    worker = pipeline(store)
    original = store._transact
    competitor_id = None

    def interleave(items):
        nonlocal competitor_id
        if competitor_id is None:
            competitor_id = "in progress"
            competitor_id = worker.process(event("competitor", "order", "errors", second=1))
        return original(items)

    store._transact = interleave
    incident_id = worker.process(event("primary"))
    assert incident_id == competitor_id
    assert incident_id == "INC-1001"
    assert len(store.list_incidents()) == 1
    assert len([entry for entry in store.timeline(incident_id) if entry.kind == "alarm"]) == 2


def test_concurrent_unrelated_alarms_make_two_gapless_incidents(store):
    worker = pipeline(store)
    original = store._transact
    competitor_id = None

    def interleave(items):
        nonlocal competitor_id
        if competitor_id is None:
            competitor_id = "in progress"
            competitor_id = worker.process(event("inventory", "inventory", second=1))
        return original(items)

    store._transact = interleave
    payment_id = worker.process(event("payment"))
    assert {payment_id, competitor_id} == {"INC-1001", "INC-1002"}
    assert len(store.list_incidents()) == 2


def test_concurrent_attachments_keep_every_alert_and_root(store):
    worker = pipeline(store)
    incident_id = worker.process(event("payment"))
    original = store._transact
    entered = False

    def interleave(items):
        nonlocal entered
        if not entered:
            entered = True
            assert worker.process(event("gateway", "gateway", "errors", second=2)) == incident_id
        return original(items)

    store._transact = interleave
    assert worker.process(event("order", "order", "latency", second=1)) == incident_id
    incident = store.get(incident_id)
    assert {alert.service for alert in incident.alerts} == {"payment", "order", "gateway"}
    assert incident.probable_root == "payment"
    assert len(store.timeline(incident_id)) == 6


def test_retry_does_not_duplicate_incident_or_timeline(store):
    worker = pipeline(store)
    alarm = event("same")
    first = worker.process(alarm)
    assert worker.process(alarm) == first
    assert len(store.list_incidents()) == 1
    assert len(store.timeline(first)) == 2


def test_sqs_failure_can_retry_without_duplicate_evidence_event(store):
    worker = pipeline(store)
    original_send = worker.sqs.send_message
    calls = 0

    def fail_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary SQS outage")
        return original_send(**kwargs)

    worker.sqs.send_message = fail_once
    with pytest.raises(RuntimeError):
        worker.process(event("retry"))
    incident_id = worker.process(event("retry"))
    assert len(store.list_incidents()) == 1
    assert len(store.timeline(incident_id)) == 2
    assert len(worker.sqs.updates()) == 2
    assert len(worker.sqs.backfills()) == 1


def test_evidence_failure_still_notifies_dashboard(store):
    worker = pipeline(store)

    def fail(**kwargs):
        raise RuntimeError("CloudWatch Logs unavailable")

    worker.logs.start_query = fail
    with pytest.raises(RuntimeError):
        worker.process(event("logs-down"))
    assert len(store.list_incidents()) == 1
    assert worker.sqs.updates() == [{"incident_id": "INC-1001", "event_id": "logs-down"}]


def test_evidence_queries_cover_window_and_hide_injection(store):
    worker = pipeline(store)
    worker.process(event("query"))
    assert len(worker.logs.queries) == 2
    assert {query["queryString"] for query in worker.logs.queries} == {SUMMARY_QUERY, SAMPLES_QUERY}
    for query in worker.logs.queries:
        assert query["endTime"] - query["startTime"] == 600
        assert "message not like /^chaos/" in query["queryString"]
        assert 'endpoint != "/chaos"' in query["queryString"]
        assert "simulation" in query["queryString"]
    assert "sort @timestamp asc" in SAMPLES_QUERY
    assert "bin(1m)" in SUMMARY_QUERY


def test_delayed_backfill_replaces_partial_evidence(store):
    worker = pipeline(store)
    alarm = event("backfill")
    incident_id = worker.process(alarm)
    key = f"incidents/{incident_id}/events/backfill/logs.json"
    assert worker.s3.objects[key]["window"]["complete"] is False
    assert worker.backfill({"incident_id": incident_id, "event": alarm}) == incident_id
    assert worker.s3.objects[key]["window"]["complete"] is True
    assert len(store.timeline(incident_id)) == 2
    assert len(worker.sqs.updates()) == 3


def test_ok_is_recorded_without_resolving(store):
    worker = pipeline(store)
    incident_id = worker.process(event("a"))
    assert worker.process(event("b", state="OK", second=1)) == incident_id
    assert store.get(incident_id).status == Status.OPEN
    assert [entry.kind for entry in store.timeline(incident_id)] == ["alarm", "evidence", "alarm"]


def test_late_alarm_does_not_undo_a_newer_ok(store):
    worker = pipeline(store)
    assert worker.process(event("recovered", state="OK", second=2)) is None
    assert worker.process(event("late", second=1)) is None
    assert store.list_incidents() == []


def test_same_timestamp_duplicate_state_change_is_ignored(store):
    worker = pipeline(store)
    incident_id = worker.process(event("first"))
    assert worker.process(event("other-event-id")) is None
    assert len(store.timeline(incident_id)) == 2


def test_resolved_incident_is_not_reused(store):
    worker = pipeline(store)
    first = worker.process(event("a"))
    store.update(first, {"status": Status.RESOLVED}, [("status", "Resolved")], "tester")
    second = worker.process(event("b", "order", "errors", second=1))
    assert first != second


def test_missing_health_stream_only_alerts_while_instance_runs(store):
    worker = pipeline(store)
    worker.ec2.state = "stopped"
    assert worker.process(event("stopped", signal="health", state="INSUFFICIENT_DATA")) is None
    assert store.list_incidents() == []
    worker.ec2.state = "running"
    incident_id = worker.process(event("running", signal="health", state="INSUFFICIENT_DATA"))
    assert store.get(incident_id).title.startswith("Monitoring degraded")
    assert store.get(incident_id).status == Status.OPEN
