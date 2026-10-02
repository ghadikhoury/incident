import httpx
import pytest
from fastapi.testclient import TestClient

import order.app as order_app
from common.errors import ServiceError

client = TestClient(order_app.app)

TIMEOUT = ServiceError(504, "DependencyTimeout", "payment did not respond")
CREATED_AT = "2026-10-02T12:00:00+00:00"


def stored_payment(order_id: str, item_id="sku-1", quantity=3, amount=30.0) -> dict:
    """A payment row as payment stores it: the charge plus the order's identity."""
    return {
        "payment_id": 7,
        "order_id": order_id,
        "amount": amount,
        "item_id": item_id,
        "quantity": quantity,
        "status": "captured",
        "created_at": CREATED_AT,
    }


@pytest.fixture
def downstream(monkeypatch):
    """Fake inventory and payment. Payment keeps an in-memory table (``payments``), so
    lookups return exactly what was stored. ``results`` overrides a path's response with an
    httpx.Response or a ServiceError to raise. Every call is recorded as (method, path, json).
    """
    results = {}
    payments: dict[str, dict] = {}
    calls = []

    def fake_call(method, url, *, dependency, timeout, json=None):
        path = httpx.URL(url).path
        calls.append((method, path, json))
        if path in results:
            if isinstance(results[path], ServiceError):
                raise results[path]
            return results[path]
        if path == "/inventory/quote":
            return httpx.Response(200, json={"unit_price": 10.0, "total": 30.0})
        if path == "/payments":
            if json["order_id"] in payments:
                return httpx.Response(200, json=payments[json["order_id"]])
            payments[json["order_id"]] = stored_payment(**json)
            return httpx.Response(201, json=payments[json["order_id"]])
        if path.startswith("/payments/by-order/"):
            order_id = path.rsplit("/", 1)[-1]
            if order_id in payments:
                return httpx.Response(200, json=payments[order_id])
            return httpx.Response(404, json={"error": "PaymentNotFound"})
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(order_app, "call", fake_call)
    return results, payments, calls


def place(order_id=None, item_id="sku-1", quantity=3):
    body = {"item_id": item_id, "quantity": quantity}
    if order_id:
        body["order_id"] = order_id
    return client.post("/orders", json=body)


def test_create_order_happy_path(downstream):
    _, _, calls = downstream
    response = place()
    assert response.status_code == 201
    order = response.json()
    assert order["amount"] == 30.0
    assert order["payment_id"] == 7
    assert order["status"] == "confirmed"
    assert client.get(f"/orders/{order['order_id']}").json() == order
    assert [path for _, path, _ in calls[:2]] == ["/inventory/quote", "/payments"]


def test_payment_receives_the_full_order_identity(downstream):
    _, _, calls = downstream
    order = place(order_id="client-key-1").json()
    assert order["order_id"] == "client-key-1"
    assert calls[1][2] == {
        "order_id": "client-key-1",
        "amount": 30.0,
        "item_id": "sku-1",
        "quantity": 3,
    }


def test_retrying_a_paid_order_returns_200_with_the_same_payment(downstream):
    first = place(order_id="client-key-2")
    retry = place(order_id="client-key-2")
    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.json() == first.json()


def test_payment_timeout_propagates(downstream):
    results, _, _ = downstream
    results["/payments"] = TIMEOUT
    response = place()
    assert response.status_code == 504
    assert response.json()["error"] == "DependencyTimeout"


def test_timed_out_order_is_confirmed_once_its_charge_lands(downstream):
    """The outcome of a timed-out order is unknown, not failed: payment has the truth."""
    results, payments, _ = downstream
    results["/payments"] = TIMEOUT
    place(order_id="late-charge", item_id="sku-2", quantity=5)

    # The slow charge hasn't landed: the order doesn't exist (yet).
    assert client.get("/orders/late-charge").status_code == 404

    # Later the charge commits, with the identity payment was sent.
    payments["late-charge"] = stored_payment("late-charge", item_id="sku-2", quantity=5)
    order = client.get("/orders/late-charge").json()
    assert order["status"] == "confirmed"
    assert (order["item_id"], order["quantity"]) == ("sku-2", 5)


def test_order_lookup_needs_no_local_state(downstream):
    """The order service keeps nothing in memory, so a restart loses nothing."""
    _, payments, _ = downstream
    payments["from-before-a-restart"] = stored_payment(
        "from-before-a-restart", item_id="sku-4", quantity=2, amount=240.0
    )
    order = client.get("/orders/from-before-a-restart").json()
    assert order["status"] == "confirmed"
    assert (order["item_id"], order["quantity"], order["amount"]) == ("sku-4", 2, 240.0)


def test_idempotency_conflict_is_passed_through(downstream):
    results, _, _ = downstream
    results["/payments"] = httpx.Response(
        409, json={"error": "IdempotencyConflict", "message": "different amount"}
    )
    response = place(order_id="reused-key")
    assert response.status_code == 409
    assert response.json()["error"] == "IdempotencyConflict"


def test_unknown_item_is_passed_through_without_charging(downstream):
    results, _, calls = downstream
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
