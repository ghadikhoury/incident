"""Integration tests against the running docker compose stack.

docker compose up -d --build --wait
pytest integration
"""

import os
import uuid

import httpx
import pytest

GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:8090")
PAYMENT = os.getenv("PAYMENT_URL", "http://127.0.0.1:8092")


@pytest.fixture(scope="module")
def http():
    with httpx.Client(timeout=10) as client:
        yield client


def new_order_id() -> str:
    return f"it-{uuid.uuid4().hex[:12]}"


def test_order_through_gateway_charges_payment(http):
    response = http.post(f"{GATEWAY}/orders", json={"item_id": "sku-4", "quantity": 2})
    assert response.status_code == 201, response.text
    order = response.json()
    assert (order["amount"], order["status"]) == (240.0, "confirmed")
    payment = http.get(f"{PAYMENT}/payments/by-order/{order['order_id']}").json()
    assert payment["payment_id"] == order["payment_id"]
    assert http.get(f"{GATEWAY}/orders/{order['order_id']}").json() == order


def test_retrying_an_order_does_not_charge_twice(http):
    body = {"item_id": "sku-1", "quantity": 2, "order_id": new_order_id()}
    first = http.post(f"{GATEWAY}/orders", json=body)
    retry = http.post(f"{GATEWAY}/orders", json=body)
    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.json()["payment_id"] == first.json()["payment_id"]


def test_reusing_an_order_id_for_a_different_order_is_rejected(http):
    order_id = new_order_id()
    http.post(f"{GATEWAY}/orders", json={"item_id": "sku-1", "quantity": 1, "order_id": order_id})
    response = http.post(
        f"{GATEWAY}/orders", json={"item_id": "sku-4", "quantity": 1, "order_id": order_id}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "IdempotencyConflict"


def test_same_amount_different_items_conflict_and_canonical_details_survive(http):
    order_id = new_order_id()
    first = http.post(
        f"{GATEWAY}/orders", json={"item_id": "sku-2", "quantity": 17, "order_id": order_id}
    )
    assert first.status_code == 201, first.text
    # Both quotes total 416.50, so amount alone cannot define an order's identity.
    conflict = http.post(
        f"{GATEWAY}/orders", json={"item_id": "sku-3", "quantity": 98, "order_id": order_id}
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"] == "IdempotencyConflict"
    order = http.get(f"{GATEWAY}/orders/{order_id}")
    assert order.status_code == 200, order.text
    assert (order.json()["item_id"], order.json()["quantity"]) == ("sku-2", 17)
    assert order.json()["payment_id"] == first.json()["payment_id"]


def test_payment_retry_does_not_charge_twice(http):
    body = {"order_id": new_order_id(), "amount": 19.98}
    first = http.post(f"{PAYMENT}/payments", json=body)
    retry = http.post(f"{PAYMENT}/payments", json=body)
    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.json()["payment_id"] == first.json()["payment_id"]


def test_unknown_order_is_404(http):
    response = http.get(f"{GATEWAY}/orders/{new_order_id()}")
    assert response.status_code == 404
    assert response.json()["error"] == "OrderNotFound"
