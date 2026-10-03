import threading
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
    assert client.get("/chaos").json() == {
        "latency_ms": 0,
        "error_rate": 0.0,
        "db_delay_s": 0.0,
        "intermittent_on_s": 0,
        "intermittent_off_s": 0,
        "cpu_ms": 0,
    }
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
    assert client.post("/chaos", json={"cpu_ms": 5001}).status_code == 422


def test_intermittent_fails_in_bursts_and_resets():
    client.post("/chaos", json={"intermittent_on_s": 120, "intermittent_off_s": 60})
    assert client.get("/work").status_code == 500
    chaos._intermittent_started = time.monotonic() - 125
    assert client.get("/work").status_code == 200
    chaos._intermittent_started = time.monotonic() - 185
    assert client.get("/work").status_code == 500
    client.delete("/chaos")
    assert client.get("/work").status_code == 200


def test_cpu_work_is_bounded_and_clears():
    client.post("/chaos", json={"cpu_ms": 30})
    start = time.perf_counter()
    assert client.get("/work").status_code == 200
    assert time.perf_counter() - start >= 0.025
    client.delete("/chaos")
    assert client.get("/chaos").json()["cpu_ms"] == 0


def test_queued_cpu_work_stops_after_reset():
    start = time.perf_counter()
    chaos._burn_cpu(5000)
    assert time.perf_counter() - start < 0.1


def test_running_cpu_work_stops_after_reset():
    chaos.state.cpu_ms = 5000
    started = threading.Event()
    stopped = threading.Event()

    def run():
        started.set()
        chaos._burn_cpu(5000)
        stopped.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert started.wait(1)
        time.sleep(0.05)
        chaos.state.cpu_ms = 0
        assert stopped.wait(1)
    finally:
        chaos.state.cpu_ms = 0
        worker.join(timeout=1)


def test_crash_exits_process_after_responding(monkeypatch):
    exits = []
    monkeypatch.setattr(chaos, "_exit_process", lambda: exits.append(True))
    response = client.post("/chaos", json={"crash": True})
    assert response.status_code == 200
    assert exits == [True]
