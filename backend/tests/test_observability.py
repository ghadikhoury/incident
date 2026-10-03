import io
import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from incident_api.config import MonitoredService
from incident_api.main import create_app
from incident_api.models import IncidentCreate
from incident_api.observability import IncidentTelemetry


class FakeCloudWatch:
    def __init__(self):
        self.requests = []

    def get_metric_data(self, **kwargs):
        self.requests.append(kwargs)
        query = kwargs["MetricDataQueries"][0]
        return {
            "MetricDataResults": [
                {
                    "Id": query["Id"],
                    "Timestamps": [datetime(2026, 10, 3, tzinfo=UTC)],
                    "Values": [9.0],
                }
            ]
        }


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.reads = []

    def get_object(self, **kwargs):
        self.reads.append(kwargs)
        return {"Body": io.BytesIO(json.dumps(self.objects[kwargs["Key"]]).encode())}


class FakeLogs:
    def __init__(self):
        self.query = None

    def start_query(self, **kwargs):
        self.query = kwargs
        return {"queryId": "q-1"}

    def get_query_results(self, **kwargs):
        assert kwargs["queryId"] == "q-1"
        return {
            "status": "Complete",
            "results": [
                [
                    {"field": "@timestamp", "value": "2026-10-03T00:00:00Z"},
                    {"field": "error_type", "value": "PoolTimeout"},
                ]
            ],
        }


def test_telemetry_limits_services_and_queries(store):
    incident = store.create(IncidentCreate(title="Failure", service="payment"))
    cloudwatch = FakeCloudWatch()
    telemetry = IncidentTelemetry(cloudwatch, FakeS3(), "evidence")
    result = telemetry.metrics(incident)
    assert result["series"] == [
        {
            "service": "payment",
            "metric": "Latency",
            "points": [{"at": "2026-10-03T00:00:00+00:00", "value": 9.0}],
        }
    ]
    assert len(cloudwatch.requests[0]["MetricDataQueries"]) == 3
    assert cloudwatch.requests[0]["MaxDatapoints"] == 5000


def test_incident_endpoints_return_saved_excerpts_and_reject_untrusted_prefix(store, fake):
    incident = store.create(IncidentCreate(title="Failure", service="payment"))
    store.record_evidence(
        incident.incident_id,
        "event-12345678",
        store.event_time(),
        "evidence",
        f"incidents/{incident.incident_id}/events/event-12345678",
    )
    store.record_evidence(
        incident.incident_id,
        "other-12345678",
        store.event_time(),
        "foreign",
        f"incidents/{incident.incident_id}/events/other-12345678",
    )
    s3 = FakeS3()
    key = f"incidents/{incident.incident_id}/events/event-12345678/logs.json"
    s3.objects[key] = {
        "error_samples": [
            {
                "@timestamp": "2026-10-03T00:00:00Z",
                "service": "payment",
                "error_type": "PoolTimeout",
            },
            {
                "@timestamp": "2026-10-03T00:00:00Z",
                "service": "payment",
                "error_type": "PoolTimeout",
            },
            {
                "@timestamp": "2026-10-03T00:01:00Z",
                "service": "payment",
                "error_type": "ConnectionError",
            },
            {"message": "chaos updated", "db_delay_s": 3},
        ]
    }
    cloudwatch_logs = FakeLogs()
    telemetry = IncidentTelemetry(FakeCloudWatch(), s3, "evidence", cloudwatch_logs)
    services = (MonitoredService("payment", "Payments", "http://payment", ()),)
    app = create_app(
        store=store,
        http_client=fake.client(),
        services=services,
        health_interval_s=3600,
        telemetry=telemetry,
    )
    with TestClient(app) as client:
        logs = client.get(f"/api/incidents/{incident.incident_id}/logs")
        metrics = client.get(f"/api/incidents/{incident.incident_id}/metrics")
        searched = client.get(
            f"/api/incidents/{incident.incident_id}/log-search", params={"q": 'Pool" | limit 999'}
        )
        assert (
            client.get(
                f"/api/incidents/{incident.incident_id}/log-search", params={"q": " "}
            ).status_code
            == 400
        )
        assert client.get("/api/incidents/INC-9999/logs").status_code == 404
    assert logs.status_code == 200
    assert logs.json()["rows"] == [
        {
            "@timestamp": "2026-10-03T00:01:00Z",
            "service": "payment",
            "error_type": "ConnectionError",
        },
        {"@timestamp": "2026-10-03T00:00:00Z", "service": "payment", "error_type": "PoolTimeout"},
    ]
    assert s3.reads == [{"Bucket": "evidence", "Key": key}]
    assert metrics.status_code == 200
    assert searched.json()["rows"][0]["error_type"] == "PoolTimeout"
    assert 'filter @message like "Pool\\" | limit 999"' in cloudwatch_logs.query["queryString"]
    assert cloudwatch_logs.query["logGroupNames"] == ["/incident/payment", "/incident/backend"]
