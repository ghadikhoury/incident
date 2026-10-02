"""Order service: reserves stock in inventory, then charges payment.

Depends on inventory and payment, so failures there propagate here.
"""

import os
import threading
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from common.app import create_app
from common.errors import ServiceError
from common.http import call

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8093")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://localhost:8092")
INVENTORY_TIMEOUT = float(os.getenv("INVENTORY_TIMEOUT", "2.0"))
PAYMENT_TIMEOUT = float(os.getenv("PAYMENT_TIMEOUT", "3.0"))
MAX_STORED_ORDERS = 10_000

app = create_app("order")

_orders: dict[str, dict] = {}
_lock = threading.Lock()


class OrderIn(BaseModel):
    item_id: str
    quantity: int = Field(gt=0, le=100)


@app.get("/health")
def health():
    return {"status": "ok", "service": "order"}


@app.post("/orders", status_code=201)
def create_order(body: OrderIn):
    order_id = uuid.uuid4().hex[:12]

    reservation = call(
        "POST",
        f"{INVENTORY_URL}/inventory/reserve",
        json=body.model_dump(),
        dependency="inventory",
        timeout=INVENTORY_TIMEOUT,
    )
    if reservation.status_code != 200:
        detail = reservation.json()
        raise ServiceError(
            reservation.status_code,
            detail.get("error", "ReservationFailed"),
            detail.get("message", f"inventory returned {reservation.status_code}"),
        )

    amount = round(reservation.json()["unit_price"] * body.quantity, 2)
    payment = call(
        "POST",
        f"{PAYMENT_URL}/payments",
        json={"order_id": order_id, "amount": amount},
        dependency="payment",
        timeout=PAYMENT_TIMEOUT,
    )
    if payment.status_code != 201:
        raise ServiceError(502, "PaymentRejected", f"payment returned {payment.status_code}")

    order = {
        "order_id": order_id,
        "item_id": body.item_id,
        "quantity": body.quantity,
        "amount": amount,
        "payment_id": payment.json()["payment_id"],
        "status": "confirmed",
        "created_at": datetime.now(UTC).isoformat(),
    }
    with _lock:
        if len(_orders) >= MAX_STORED_ORDERS:
            _orders.pop(next(iter(_orders)))  # drop the oldest so memory stays bounded
        _orders[order_id] = order
    return order


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    order = _orders.get(order_id)
    if order is None:
        raise ServiceError(404, "OrderNotFound", f"no order {order_id}")
    return order
