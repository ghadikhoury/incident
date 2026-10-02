"""Order service: gets a price quote from inventory, then charges payment.

Depends on inventory and payment, so failures there propagate here.

Consistency model: an order exists exactly when payment has captured a charge for its
order_id, so there is no separate state that a failure could leave half-done.
- Clients may send their own order_id as an idempotency key; retrying the same order
  never charges twice (payment is idempotent per order_id).
- A failed request may have an unknown outcome (e.g. it timed out while the charge was
  still being written). GET /orders/{order_id} asks payment for the truth, so a charge
  that landed late shows up as a confirmed order.
"""

import os
import uuid

import httpx
from fastapi import Response
from pydantic import BaseModel, Field

from common.app import create_app
from common.errors import ServiceError
from common.http import call

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8093")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://localhost:8092")
INVENTORY_TIMEOUT = float(os.getenv("INVENTORY_TIMEOUT", "2.0"))
PAYMENT_TIMEOUT = float(os.getenv("PAYMENT_TIMEOUT", "3.0"))
app = create_app("order")


class OrderIn(BaseModel):
    item_id: str
    quantity: int = Field(gt=0, le=100)
    order_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


@app.get("/health")
def health():
    return {"status": "ok", "service": "order"}


@app.post("/orders", status_code=201)
def create_order(body: OrderIn, response: Response):
    """201 for a new order; 200 if this order_id was already placed and paid."""
    order_id = body.order_id or uuid.uuid4().hex[:12]

    quote = call(
        "POST",
        f"{INVENTORY_URL}/inventory/quote",
        json={"item_id": body.item_id, "quantity": body.quantity},
        dependency="inventory",
        timeout=INVENTORY_TIMEOUT,
    )
    _raise_unless_ok(quote, "QuoteFailed")
    amount = quote.json()["total"]

    payment = call(
        "POST",
        f"{PAYMENT_URL}/payments",
        json={
            "order_id": order_id,
            "amount": amount,
            "item_id": body.item_id,
            "quantity": body.quantity,
        },
        dependency="payment",
        timeout=PAYMENT_TIMEOUT,
    )
    _raise_unless_ok(payment, "PaymentFailed")

    confirmed = _confirm(payment.json())
    if payment.status_code == 200:
        response.status_code = 200
    return confirmed


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    # Payment is the durable source of both the charge and the order identity.
    payment = call(
        "GET",
        f"{PAYMENT_URL}/payments/by-order/{order_id}",
        dependency="payment",
        timeout=PAYMENT_TIMEOUT,
    )
    if payment.status_code == 404:
        raise ServiceError(404, "OrderNotFound", f"no order {order_id}")
    _raise_unless_ok(payment, "PaymentLookupFailed")
    return _confirm(payment.json())


def _confirm(payment: dict) -> dict:
    return {
        "order_id": payment["order_id"],
        "item_id": payment["item_id"],
        "quantity": payment["quantity"],
        "amount": payment["amount"],
        "payment_id": payment["payment_id"],
        "status": "confirmed",
        "created_at": payment["created_at"],
    }


def _raise_unless_ok(response: httpx.Response, fallback_error: str) -> None:
    """Pass a dependency's 4xx (e.g. ItemNotFound, IdempotencyConflict) through to our caller."""
    if response.status_code in (200, 201):
        return
    try:
        detail = response.json()
    except ValueError:
        detail = {}
    raise ServiceError(
        response.status_code,
        detail.get("error", fallback_error),
        detail.get("message", f"dependency returned HTTP {response.status_code}"),
    )
