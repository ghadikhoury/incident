import pytest
from fastapi.testclient import TestClient

from incident_api.config import MonitoredService
from incident_api.main import create_app

FAILURE_URL = "/api/simulation/failure"
SERVICES = (
    MonitoredService("payment", "Payments", "http://payment", ("postgres",)),
    MonitoredService("order", "Orders", "http://order", ("payment",)),
)


@pytest.fixture
def client(store, fake):
    app = create_app(
        store=store, http_client=fake.client(), services=SERVICES, health_interval_s=3600
    )
    with TestClient(app) as test_client:
        yield test_client


def create(client, **overrides):
    body = {"title": "Payment latency spike", "service": "payment", "severity": "SEV-2"}
    response = client.post("/api/incidents", json=body | overrides)
    assert response.status_code == 201
    return response.json()


def test_create_and_get_incident_with_timeline(client):
    incident = create(client, actor="alice")
    detail = client.get(f"/api/incidents/{incident['incident_id']}").json()
    assert detail["title"] == "Payment latency spike"
    assert detail["status"] == "OPEN"
    assert detail["timeline"][0]["kind"] == "created"
    assert detail["timeline"][0]["actor"] == "alice"


def test_unknown_incident_is_404(client):
    assert client.get("/api/incidents/INC-9999").status_code == 404
    assert client.get("/api/incidents/INC-9999/timeline").status_code == 404
    assert client.patch("/api/incidents/INC-9999", json={"severity": "SEV-1"}).status_code == 404


def test_patch_updates_fields_and_records_timeline(client):
    incident_id = create(client)["incident_id"]
    response = client.patch(
        f"/api/incidents/{incident_id}",
        json={"severity": "SEV-1", "assigned_to": "bob", "actor": "carol"},
    )
    assert response.status_code == 200
    assert response.json()["severity"] == "SEV-1"
    timeline = client.get(f"/api/incidents/{incident_id}/timeline").json()
    messages = [e["message"] for e in timeline]
    assert "Severity set to SEV-1" in messages
    assert "Assigned to bob" in messages
    assert {e["actor"] for e in timeline[1:]} == {"carol"}


@pytest.mark.parametrize(
    "body",
    [{}, {"status": "OPEN"}, {"severity": None}, {"actor": "only-actor"}],
)
def test_patch_rejects_invalid_changes(client, body):
    incident_id = create(client)["incident_id"]
    assert client.patch(f"/api/incidents/{incident_id}", json=body).status_code == 400


def test_acknowledge_only_once(client):
    incident_id = create(client)["incident_id"]
    url = f"/api/incidents/{incident_id}/acknowledge"
    first = client.post(url, json={"actor": "alice", "note": "looking"})
    assert first.status_code == 200
    assert first.json()["status"] == "ACKNOWLEDGED"
    assert client.post(url, json={}).status_code == 409
    messages = [e["message"] for e in client.get(f"/api/incidents/{incident_id}/timeline").json()]
    assert "Acknowledged: looking" in messages


def test_resolved_incident_cannot_change(client):
    incident_id = create(client)["incident_id"]
    resolved = client.post(f"/api/incidents/{incident_id}/resolve", json={})
    assert resolved.json()["status"] == "RESOLVED"
    assert resolved.json()["resolved_at"]
    response = client.patch(f"/api/incidents/{incident_id}", json={"severity": "SEV-4"})
    assert response.status_code == 409


def test_list_active_hides_resolved(client):
    open_id = create(client, title="still open")["incident_id"]
    done_id = create(client, title="done")["incident_id"]
    client.post(f"/api/incidents/{done_id}/resolve", json={})
    assert [i["incident_id"] for i in client.get("/api/incidents?active=true").json()] == [open_id]
    assert len(client.get("/api/incidents").json()) == 2


def next_incident_message(ws) -> dict:
    """Skip health broadcasts (the startup poll may land at any moment)."""
    while (message := ws.receive_json())["type"] != "incident":
        assert message["type"] == "services"
    return message


def test_websocket_receives_services_then_incident_updates(client):
    with client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "services"
        incident = create(client)
        assert next_incident_message(ws) == {"type": "incident", "data": incident}
        client.post(f"/api/incidents/{incident['incident_id']}/acknowledge", json={})
        assert next_incident_message(ws)["data"]["status"] == "ACKNOWLEDGED"


def test_inject_and_recover_failure(client, fake):
    response = client.post(
        "/api/simulation/failure", json={"service": "payment", "failure": "db_slow"}
    )
    assert response.status_code == 200
    assert fake.chaos["payment"]["db_delay_s"] == 3.0
    payment = {s["name"]: s for s in client.get("/api/services").json()}["payment"]
    assert payment["chaos"]["db_delay_s"] == 3.0

    assert client.post("/api/simulation/recover", json={"service": "payment"}).status_code == 200
    assert fake.chaos["payment"]["db_delay_s"] == 0.0


def test_db_failure_needs_a_database(client):
    response = client.post(
        "/api/simulation/failure", json={"service": "order", "failure": "db_slow"}
    )
    assert response.status_code == 400


def test_unknown_service_is_404(client):
    response = client.post(
        "/api/simulation/failure", json={"service": "nope", "failure": "latency"}
    )
    assert response.status_code == 404


def test_crashed_service_cannot_be_recovered_through_chaos(client, fake):
    client.post(FAILURE_URL, json={"service": "payment", "failure": "crash"})
    response = client.post("/api/simulation/recover", json={"service": "payment"})
    assert response.status_code == 409
    assert "docker compose start payment" in response.json()["detail"]
