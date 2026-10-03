"""The alarm pipeline's durable behavior, using an in-memory DynamoDB table."""

import json
from datetime import UTC, datetime

import pytest

from incident_api.models import Status
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


def event(event_id, service="payment", signal="latency", state="ALARM", second=0):
    return {
        "id": event_id,
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": f"incident-{service}-{signal}",
            "state": {
                "value": state,
                "timestamp": datetime(2026, 10, 2, 12, 0, second, tzinfo=UTC).isoformat(),
            },
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
                ("inventory", "latency"),
                ("payment", "health"),
            ]
        )
    ]
    ids = [worker.process(alarm) for alarm in events]
    assert len(set(ids)) == 1
    assert len(store.list_incidents()) == 1
    assert len([entry for entry in store.timeline(ids[0]) if entry.kind == "alarm"]) == 5


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
