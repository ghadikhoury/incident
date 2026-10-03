import io
import json
import time
from datetime import UTC, datetime
from queue import Empty, Queue

import pytest
from fastapi.testclient import TestClient

from incident_api.config import MonitoredService
from incident_api.diagnosis.engine import DiagnosisEngine
from incident_api.main import create_app
from incident_api.models import Diagnosis, IncidentCreate, RecommendedAction


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, *, Bucket, Key):
        assert Bucket == "test-evidence"
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}


class FakeBedrock:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": json.dumps(self.output)}]}},
        }


def ready_diagnosis(claim):
    return Diagnosis(
        status="READY",
        claimed_at=claim,
        summary="Payment is slow",
        likely_root_cause="Connection pool exhaustion",
        confidence="high",
        evidence=["payment pool timeout at 10:00"],
        recommended_actions=[
            RecommendedAction(
                id="clear_chaos:payment",
                action="clear_chaos",
                service="payment",
                reason="Reset the simulated fault",
            )
        ],
    )


def test_diagnosis_reads_evidence_and_ignores_injection_lines(store):
    incident = store.create(IncidentCreate(title="payment alarm", service="payment"))
    store.record_evidence(
        incident.incident_id,
        "event-12345678",
        datetime.now(UTC).isoformat(),
        "test-evidence",
        f"incidents/{incident.incident_id}/events/event-12345678",
    )
    prefix = f"incidents/{incident.incident_id}/events/event-12345678"
    s3 = FakeS3(
        {
            f"{prefix}/logs.json": {
                "window": {"start": "2026-10-03T10:00:00Z", "complete": False},
                "minute_summary": [{"minute": "10:01", "events": "5"}],
                "error_samples": [
                    {"message": "chaos updated db_delay_s=3.0"},
                    {"message": "payment connection pool timeout"},
                ],
            },
            f"{prefix}/metrics.json": {"MetricDataResults": [{"Id": "latency", "Values": [3000]}]},
        }
    )
    bedrock = FakeBedrock(
        {
            "summary": "Payment requests fail when all connections are occupied",
            "likely_root_cause": "Payment connection-pool exhaustion",
            "confidence": "high",
            "evidence": ["payment connection pool timeout"],
            "recommended_actions": [
                {"action": "clear_chaos", "service": "payment", "reason": "Reset fault"},
                {"action": "clear_chaos", "service": "order", "reason": "Wrong target"},
            ],
        }
    )
    engine = DiagnosisEngine(s3, bedrock, "test-evidence", "test-model")
    claim = store.claim_diagnosis(incident.incident_id)
    assert claim
    assert store.claim_diagnosis(incident.incident_id) is None
    result = engine.analyze(
        store.get(incident.incident_id),
        store.timeline(incident.incident_id),
        {"payment", "order"},
        claim,
    )
    assert result.likely_root_cause == "Payment connection-pool exhaustion"
    assert [action.id for action in result.recommended_actions] == ["clear_chaos:payment"]
    request = bedrock.calls[0]
    prompt = request["messages"][0]["content"][0]["text"]
    assert "connection pool timeout" in prompt
    assert "chaos updated" not in prompt
    assert request["outputConfig"]["textFormat"]["type"] == "json_schema"
    saved = store.finish_diagnosis(incident.incident_id, claim, result)
    assert saved.diagnosis.status == "READY"
    assert store.finish_diagnosis(incident.incident_id, claim, result) is None
    assert [event.kind for event in store.timeline(incident.incident_id)].count("diagnosis") == 2


def test_invalid_model_output_never_becomes_a_recommendation(store):
    incident = store.create(IncidentCreate(title="payment alarm", service="payment"))
    prefix = f"incidents/{incident.incident_id}/events/event-12345678"
    store.record_evidence(
        incident.incident_id,
        "event-12345678",
        datetime.now(UTC).isoformat(),
        "test-evidence",
        prefix,
    )
    s3 = FakeS3({f"{prefix}/logs.json": {}, f"{prefix}/metrics.json": {}})
    bedrock = FakeBedrock(
        {
            "summary": "x",
            "likely_root_cause": "x",
            "confidence": "high",
            "evidence": [],
            "recommended_actions": [{"action": "run_shell", "service": "payment", "reason": "bad"}],
        }
    )
    with pytest.raises(ValueError):
        DiagnosisEngine(s3, bedrock, "test-evidence", "model").analyze(
            incident, store.timeline(incident.incident_id), {"payment"}, "claim"
        )


@pytest.fixture
def api(store, fake):
    services = (MonitoredService("payment", "Payment", "http://payment", ()),)
    app = create_app(
        store=store,
        http_client=fake.client(),
        services=services,
        health_interval_s=3600,
    )
    with TestClient(app) as client:
        yield client


def prepare(store):
    incident = store.create(IncidentCreate(title="payment alarm", service="payment"))
    claim = store.claim_diagnosis(incident.incident_id)
    store.finish_diagnosis(incident.incident_id, claim, ready_diagnosis(claim))
    return incident.incident_id


def test_reject_records_decision_without_running_remediation(api, store, fake):
    incident_id = prepare(store)
    url = f"/api/incidents/{incident_id}/recommendations/clear_chaos:payment"
    fake.chaos["payment"]["db_delay_s"] = 3.0
    response = api.post(url + "/reject", json={"actor": "alice"})
    assert response.status_code == 200
    assert response.json()["diagnosis"]["recommended_actions"][0]["status"] == "REJECTED"
    assert fake.chaos["payment"]["db_delay_s"] == 3.0
    assert api.post(url + "/approve", json={"actor": "bob"}).status_code == 409
    timeline = api.get(f"/api/incidents/{incident_id}/timeline").json()
    assert any(
        event["message"] == "Rejected remediation: clear_chaos:payment"
        and event["actor"] == "alice"
        for event in timeline
    )


def test_approve_recovers_only_once_and_requires_actor(api, store, fake):
    incident_id = prepare(store)
    url = f"/api/incidents/{incident_id}/recommendations/clear_chaos:payment"
    fake.chaos["payment"]["db_delay_s"] = 3.0
    assert api.post(url + "/approve", json={}).status_code == 422
    assert api.post(url + "/approve", json={"actor": "  "}).status_code == 422
    assert fake.chaos["payment"]["db_delay_s"] == 3.0
    response = api.post(url + "/approve", json={"actor": "alice"})
    assert response.status_code == 200
    assert response.json()["diagnosis"]["recommended_actions"][0]["status"] == "SUCCEEDED"
    assert fake.chaos["payment"]["db_delay_s"] == 0.0
    assert api.post(url + "/approve", json={"actor": "alice"}).status_code == 409
    timeline = api.get(f"/api/incidents/{incident_id}/timeline").json()
    assert [event["kind"] for event in timeline].count("remediation") == 3


def test_failed_approved_remediation_is_visible_and_can_be_retried(api, store, fake):
    incident_id = prepare(store)
    url = f"/api/incidents/{incident_id}/recommendations/clear_chaos:payment/approve"
    fake.down.add("payment")
    first = api.post(url, json={"actor": "alice"})
    assert first.status_code == 200
    assert first.json()["diagnosis"]["recommended_actions"][0]["status"] == "FAILED"
    fake.down.clear()
    second = api.post(url, json={"actor": "bob"})
    assert second.status_code == 200
    assert second.json()["diagnosis"]["recommended_actions"][0]["status"] == "SUCCEEDED"
    assert second.json()["diagnosis"]["recommended_actions"][0]["decided_by"] == "bob"


def test_execution_outcome_is_recorded_if_incident_resolves_mid_action(store):
    incident_id = prepare(store)
    action_id = "clear_chaos:payment"
    store.change_recommendation(
        incident_id, 0, action_id, "PENDING", "APPROVED", "alice", "Approved"
    )
    store.change_recommendation(
        incident_id, 0, action_id, "APPROVED", "EXECUTING", "alice", "Started"
    )
    store.update(incident_id, {"status": "RESOLVED"}, [("status", "Resolved")], "alice")
    result = store.change_recommendation(
        incident_id, 0, action_id, "EXECUTING", "SUCCEEDED", "alice", "Succeeded"
    )
    assert result.status == "RESOLVED"
    assert result.diagnosis.recommended_actions[0].status == "SUCCEEDED"


@pytest.mark.parametrize("fails", [False, True])
def test_background_analysis_survives_model_failure(store, fake, fails):
    incident = store.create(IncidentCreate(title="alarm", service="payment"))
    store.record_evidence(
        incident.incident_id,
        "event-12345678",
        datetime.now(UTC).isoformat(),
        "test-evidence",
        f"incidents/{incident.incident_id}/events/event-12345678",
    )

    class Engine:
        def analyze(self, incident, timeline, services, claim):
            if fails:
                raise RuntimeError("Bedrock is unavailable")
            assert any(event.kind == "evidence" for event in timeline)
            return ready_diagnosis(claim)

    class QueueClient:
        def __init__(self):
            self.messages = Queue()

        def receive_message(self, **kwargs):
            try:
                return {"Messages": [self.messages.get(timeout=0.05)]}
            except Empty:
                return {}

        def delete_message(self, **kwargs):
            pass

    app = create_app(
        store=store,
        http_client=fake.client(),
        services=(MonitoredService("payment", "Payment", "http://payment", ()),),
        health_interval_s=3600,
        queue_url="test",
        sqs_client=QueueClient(),
        diagnosis_engine=Engine(),
        diagnosis_delay_s=0,
    )
    with TestClient(app):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = store.get(incident.incident_id)
            if current.diagnosis and current.diagnosis.status != "RUNNING":
                break
            time.sleep(0.02)
        assert current.diagnosis.status == ("UNAVAILABLE" if fails else "READY")
        assert current.status == "OPEN"
    events = store.timeline(incident.incident_id)
    assert any(
        event.message == ("AI analysis unavailable" if fails else "AI analysis ready for review")
        for event in events
    )
