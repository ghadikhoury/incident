import httpx
import pytest
from fastapi.testclient import TestClient

import order.app as order_app
from common.errors import ServiceError

client = TestClient(order_app.app)

TIMEOUT = ServiceError(504, "DependencyTimeout", "payment did not respond")
PAYMENT = {"payment_id": 7, "amount": 30.0, "created_at": "2026-10-02T12:00:00+00:00"}


@pytest.fixture
def downstream(monkeypatch):
    """Fake inventory/payment, keyed by URL path prefix. A result is an httpx.Response or a
    ServiceError to raise. Records every call as (method, path, json)."""
    results = {
        "/inventory/quote": httpx.Response(200, json={"unit_price": 10.0, "total": 30.0}),
        "/payments": httpx.Response(201, json=PAYMENT),
        "/payments/by-order/": httpx.Response(404, json={"error": "PaymentNotFound"}),
    }
    calls = []
    payments = {}

    def fake_call(method, url, *, dependency, timeout, json=None):
        path = httpx.URL(url).path
        calls.append((method, path, json))
        key = "/payments/by-order/" if path.startswith("/payments/by-order/") else path
        result = results[key]
        if isinstance(result, ServiceError):
            raise result
        if path == "/payments" and result.status_code in (200, 201):
            payment = {**result.json(), **json}
            payments[json["order_id"]] = payment
            return httpx.Response(result.status_code, json=payment)
        if key == "/payments/by-order/":
            order_id = path.rsplit("/", 1)[-1]
            if result.status_code == 200:
                return httpx.Response(
                    200,
                    json={**result.json(), "order_id": order_id, "item_id": "sku-1", "quantity": 3},
                )
            if order_id in payments:
                return httpx.Response(200, json=payments[order_id])
        return result

    monkeypatch.setattr(order_app, "call", fake_call)
    return results, calls


def place(order_id=None, item_id="sku-1", quantity=3):
    body = {"item_id": item_id, "quantity": quantity}
    if order_id:
        body["order_id"] = order_id
    return client.post("/orders", json=body)


def test_create_order_happy_path(downstream):
    _, calls = downstream
    response = place()
    assert response.status_code == 201
    order = response.json()
    assert order["amount"] == 30.0
    assert order["payment_id"] == 7
    assert order["status"] == "confirmed"
    assert client.get(f"/orders/{order['order_id']}").json() == order
    assert [path for _, path, _ in calls[:2]] == ["/inventory/quote", "/payments"]
    assert calls[2][1] == f"/payments/by-order/{order['order_id']}"


def test_client_order_id_is_the_payment_idempotency_key(downstream):
    _, calls = downstream
    order = place(order_id="client-key-1").json()
    assert order["order_id"] == "client-key-1"
    assert calls[1][2] == {
        "order_id": "client-key-1",
        "amount": 30.0,
        "item_id": "sku-1",
        "quantity": 3,
    }


def test_retrying_a_paid_order_returns_200_with_the_same_payment(downstream):
    results, _ = downstream
    results["/payments"] = httpx.Response(200, json=PAYMENT)  # payment: already charged
    response = place(order_id="client-key-2")
    assert response.status_code == 200
    assert response.json()["payment_id"] == 7


def test_payment_timeout_propagates(downstream):
    results, _ = downstream
    results["/payments"] = TIMEOUT
    response = place()
    assert response.status_code == 504
    assert response.json()["error"] == "DependencyTimeout"


def test_timed_out_order_is_confirmed_once_its_charge_lands(downstream):
    """The outcome of a timed-out order is unknown, not failed: payment has the truth."""
    results, _ = downstream
    results["/payments"] = TIMEOUT
    place(order_id="late-charge", quantity=3)

    # The slow charge hasn't landed: the order doesn't exist (yet).
    assert client.get("/orders/late-charge").status_code == 404

    # Later the charge commits: the order is confirmed, with the details we recorded.
    results["/payments/by-order/"] = httpx.Response(200, json=PAYMENT)
    order = client.get("/orders/late-charge").json()
    assert order["status"] == "confirmed"
    assert order["payment_id"] == 7
    assert (order["item_id"], order["quantity"]) == ("sku-1", 3)


def test_order_unknown_to_this_process_is_resolved_from_payment(downstream):
    results, _ = downstream
    results["/payments/by-order/"] = httpx.Response(200, json=PAYMENT)
    order = client.get("/orders/from-before-a-restart").json()
    assert order["status"] == "confirmed"
    assert (order["item_id"], order["quantity"]) == ("sku-1", 3)


def test_idempotency_conflict_is_passed_through(downstream):
    results, _ = downstream
    results["/payments"] = httpx.Response(
        409, json={"error": "IdempotencyConflict", "message": "different amount"}
    )
    response = place(order_id="reused-key")
    assert response.status_code == 409
    assert response.json()["error"] == "IdempotencyConflict"


def test_unknown_item_is_passed_through_without_charging(downstream):
    results, calls = downstream
    results["/inventory/quote"] = httpx.Response(
        404, json={"error": "ItemNotFound", "message": "unknown item nope"}
    )
    response = place(item_id="nope")
    assert response.status_code == 404
    assert response.json()["error"] == "ItemNotFound"
    assert [path for _, path, _ in calls] == ["/inventory/quote"]


def test_unknown_order_is_404(downstream):
    assert client.get("/orders/nope").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"item_id": "sku-1", "quantity": 0},
        {"item_id": "sku-1", "quantity": 1, "order_id": "has spaces"},
        {"item_id": "sku-1", "quantity": 1, "order_id": "x" * 65},
    ],
)
def test_invalid_orders_are_rejected(downstream, body):
    assert client.post("/orders", json=body).status_code == 422
