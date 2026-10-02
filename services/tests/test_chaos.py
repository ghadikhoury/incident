import time

import pytest
from fastapi.testclient import TestClient

from common import chaos
from common.app import create_app

app = create_app("chaos-test")
db_app = create_app("chaos-db-test", supports_db_chaos=True)


@app.get("/work")
def work():
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}


client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_chaos():
    client.delete("/chaos")
    yield
    client.delete("/chaos")


def test_no_chaos_by_default():
    assert client.get("/chaos").json() == {"latency_ms": 0, "error_rate": 0.0, "db_delay_s": 0.0}
    assert client.get("/work").status_code == 200


def test_error_rate_one_fails_every_request():
    client.post("/chaos", json={"error_rate": 1.0})
    response = client.get("/work")
    assert response.status_code == 500
    assert response.json()["error"] == "InternalServerError"


def test_health_is_exempt_from_chaos():
    client.post("/chaos", json={"error_rate": 1.0})
    assert client.get("/health").status_code == 200


def test_latency_is_added():
    client.post("/chaos", json={"latency_ms": 200})
    start = time.perf_counter()
    client.get("/work")
    assert time.perf_counter() - start >= 0.2


def test_delete_resets_everything():
    client.post("/chaos", json={"latency_ms": 100, "error_rate": 0.5})
    client.delete("/chaos")
    assert client.get("/chaos").json()["error_rate"] == 0.0
    assert client.get("/work").status_code == 200


def test_settings_combine_across_calls():
    client.post("/chaos", json={"latency_ms": 10})
    state = client.post("/chaos", json={"error_rate": 0.25}).json()
    assert state["latency_ms"] == 10
    assert state["error_rate"] == 0.25


def test_db_delay_rejected_without_database():
    response = client.post("/chaos", json={"db_delay_s": 3})
    assert response.status_code == 400
    assert response.json()["error"] == "UnsupportedChaos"


def test_db_delay_accepted_with_database():
    response = TestClient(db_app).post("/chaos", json={"db_delay_s": 3})
    assert response.json()["db_delay_s"] == 3


def test_invalid_values_rejected():
    assert client.post("/chaos", json={"error_rate": 1.5}).status_code == 422


def test_crash_exits_process_after_responding(monkeypatch):
    exits = []
    monkeypatch.setattr(chaos, "_exit_process", lambda: exits.append(True))
    response = client.post("/chaos", json={"crash": True})
    assert response.status_code == 200
    assert exits == [True]
