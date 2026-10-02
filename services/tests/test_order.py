import httpx
import pytest
from fastapi.testclient import TestClient

import order.app as order_app
from common.errors import ServiceError

client = TestClient(order_app.app)

TIMEOUT = ServiceError(504, "DependencyTimeout", "payment did not respond")


class InlineExecutor:
    """Runs submitted work immediately, so background compensation is deterministic in tests."""

    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def downstream(monkeypatch):
    """Fake inventory/payment. Each endpoint maps to a list of results consumed in order
    (the last one repeats); a result is an httpx.Response or a ServiceError to raise."""
    results = {
        "/inventory/reserve": [httpx.Response(200, json={"unit_price": 10.0})],
        "/payments": [httpx.Response(201, json={"payment_id": 7})],
        "/payments/void": [httpx.Response(200, json={"status": "voided"})],
        "/inventory/release": [httpx.Response(200, json={"released": True})],
    }
    calls = []

    def fake_call(method, url, *, dependency, timeout, json=None):
        path = httpx.URL(url).path
        calls.append((path, json))
        queue = results[path]
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, ServiceError):
            raise result
        return result

    monkeypatch.setattr(order_app, "call", fake_call)
    monkeypatch.setattr(order_app, "_compensations", InlineExecutor())
    monkeypatch.setattr(order_app.time, "sleep", lambda seconds: None)
    return results, calls


def paths(calls):
    return [path for path, _ in calls]


def test_create_order_happy_path(downstream):
    _, calls = downstream
    response = client.post("/orders", json={"item_id": "sku-1", "quantity": 3})
    assert response.status_code == 201
    body = response.json()
    assert body["amount"] == 30.0
    assert body["payment_id"] == 7
    assert client.get(f"/orders/{body['order_id']}").json() == body
    assert paths(calls) == ["/inventory/reserve", "/payments"]


def test_reservation_and_payment_share_the_order_id(downstream):
    _, calls = downstream
    order_id = client.post("/orders", json={"item_id": "sku-1", "quantity": 1}).json()["order_id"]
    assert [body["order_id"] for _, body in calls] == [order_id, order_id]


def test_payment_timeout_voids_payment_and_releases_stock(downstream):
    results, calls = downstream
    results["/payments"] = [TIMEOUT]
    response = client.post("/orders", json={"item_id": "sku-1", "quantity": 1})
    assert response.status_code == 504
    assert response.json()["error"] == "DependencyTimeout"
    assert paths(calls) == [
        "/inventory/reserve",
        "/payments",
        "/payments/void",
        "/inventory/release",
    ]
    order_id = calls[0][1]["order_id"]
    assert calls[2][1] == {"order_id": order_id}
    assert calls[3][1] == {"order_id": order_id}


def test_rejected_payment_is_compensated(downstream):
    results, calls = downstream
    results["/payments"] = [httpx.Response(409, json={"error": "PaymentVoided"})]
    response = client.post("/orders", json={"item_id": "sku-1", "quantity": 1})
    assert response.status_code == 502
    assert response.json()["error"] == "PaymentRejected"
    assert paths(calls)[-2:] == ["/payments/void", "/inventory/release"]


def test_compensation_retries_until_dependencies_recover(downstream):
    results, calls = downstream
    results["/payments"] = [TIMEOUT]
    results["/payments/void"] = [TIMEOUT, TIMEOUT, httpx.Response(200, json={})]
    results["/inventory/release"] = [TIMEOUT, httpx.Response(200, json={})]
    client.post("/orders", json={"item_id": "sku-1", "quantity": 1})
    assert paths(calls).count("/payments/void") == 3
    assert paths(calls).count("/inventory/release") == 2


def test_stock_is_released_even_if_void_never_succeeds(downstream):
    results, calls = downstream
    results["/payments"] = [TIMEOUT]
    results["/payments/void"] = [TIMEOUT]
    client.post("/orders", json={"item_id": "sku-1", "quantity": 1})
    attempts = 1 + len(order_app.COMPENSATION_BACKOFF_S)
    assert paths(calls).count("/payments/void") == attempts
    assert paths(calls)[-1] == "/inventory/release"


def test_compensation_does_not_retry_client_errors(downstream):
    results, calls = downstream
    results["/payments"] = [TIMEOUT]
    results["/payments/void"] = [httpx.Response(422, json={})]
    client.post("/orders", json={"item_id": "sku-1", "quantity": 1})
    assert paths(calls).count("/payments/void") == 1


def test_reservation_timeout_releases_without_voiding(downstream):
    results, calls = downstream
    results["/inventory/reserve"] = [ServiceError(504, "DependencyTimeout", "slow")]
    response = client.post("/orders", json={"item_id": "sku-1", "quantity": 1})
    assert response.status_code == 504
    assert paths(calls) == ["/inventory/reserve", "/inventory/release"]


def test_out_of_stock_needs_no_compensation(downstream):
    results, calls = downstream
    results["/inventory/reserve"] = [
        httpx.Response(409, json={"error": "OutOfStock", "message": "not enough stock"})
    ]
    response = client.post("/orders", json={"item_id": "sku-1", "quantity": 1})
    assert response.status_code == 409
    assert response.json()["error"] == "OutOfStock"
    assert paths(calls) == ["/inventory/reserve"]


def test_unknown_order_is_404():
    assert client.get("/orders/nope").status_code == 404


def test_invalid_quantity_is_rejected():
    assert client.post("/orders", json={"item_id": "sku-1", "quantity": 0}).status_code == 422
