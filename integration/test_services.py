"""Integration tests against the running docker compose stack.

docker compose up -d --build --wait
pytest integration
"""

import os
import uuid

import httpx
import pytest

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8090")
PAYMENT = os.getenv("PAYMENT_URL", "http://localhost:8092")
INVENTORY = os.getenv("INVENTORY_URL", "http://localhost:8093")


@pytest.fixture(scope="module")
def http():
    with httpx.Client(timeout=10) as client:
        yield client


def new_order_id() -> str:
    return f"it-{uuid.uuid4().hex[:12]}"


def stock(http, item_id: str) -> int:
    return http.get(f"{INVENTORY}/inventory/{item_id}").json()["stock"]


def test_order_through_gateway_reserves_stock_and_charges(http):
    before = stock(http, "sku-4")
    response = http.post(f"{GATEWAY}/orders", json={"item_id": "sku-4", "quantity": 2})
    assert response.status_code == 201, response.text
    order = response.json()
    assert order["amount"] == 240.0
    assert stock(http, "sku-4") <= before - 2  # loadgen (if running) may also order sku-4
    payment = http.get(f"{PAYMENT}/payments/{order['payment_id']}").json()
    assert payment["order_id"] == order["order_id"]
    assert payment["status"] == "captured"


def test_payment_retry_does_not_charge_twice(http):
    order_id = new_order_id()
    body = {"order_id": order_id, "amount": 19.98}
    first = http.post(f"{PAYMENT}/payments", json=body)
    retry = http.post(f"{PAYMENT}/payments", json=body)
    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.json()["payment_id"] == first.json()["payment_id"]


def test_payment_retry_with_a_different_amount_is_rejected(http):
    order_id = new_order_id()
    http.post(f"{PAYMENT}/payments", json={"order_id": order_id, "amount": 10.0})
    response = http.post(f"{PAYMENT}/payments", json={"order_id": order_id, "amount": 99.0})
    assert response.status_code == 409
    assert response.json()["error"] == "IdempotencyConflict"


def test_void_before_a_late_charge_blocks_the_charge(http):
    order_id = new_order_id()
    voided = http.post(f"{PAYMENT}/payments/void", json={"order_id": order_id})
    assert voided.json()["status"] == "voided"
    late = http.post(f"{PAYMENT}/payments", json={"order_id": order_id, "amount": 10.0})
    assert late.status_code == 409
    assert late.json()["error"] == "PaymentVoided"


def test_void_after_charge_is_idempotent(http):
    order_id = new_order_id()
    charged = http.post(f"{PAYMENT}/payments", json={"order_id": order_id, "amount": 10.0}).json()
    first = http.post(f"{PAYMENT}/payments/void", json={"order_id": order_id}).json()
    second = http.post(f"{PAYMENT}/payments/void", json={"order_id": order_id}).json()
    assert first["payment_id"] == second["payment_id"] == charged["payment_id"]
    assert second["status"] == "voided"
