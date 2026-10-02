"""Order service: reserves stock in inventory, then charges payment.

Depends on inventory and payment, so failures there propagate here.

If payment fails, the order is compensated in the background: the payment is voided
(in case a slow charge still lands later) and the reserved stock is released. Both
calls are idempotent on order_id, so they are retried until they succeed.
"""

import contextvars
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from common.app import create_app
from common.errors import ServiceError
from common.http import call
from common.logs import get_logger

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8093")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://localhost:8092")
INVENTORY_TIMEOUT = float(os.getenv("INVENTORY_TIMEOUT", "2.0"))
PAYMENT_TIMEOUT = float(os.getenv("PAYMENT_TIMEOUT", "3.0"))
MAX_STORED_ORDERS = 10_000
# Seconds to wait before each compensation retry (about a minute in total).
COMPENSATION_BACKOFF_S = (1, 2, 4, 8, 15, 30)

app = create_app("order")
log = get_logger()

_orders: dict[str, dict] = {}
_lock = threading.Lock()
_compensations = ThreadPoolExecutor(max_workers=16, thread_name_prefix="compensate")


class OrderIn(BaseModel):
    item_id: str
    quantity: int = Field(gt=0, le=100)


@app.get("/health")
def health():
    return {"status": "ok", "service": "order"}


@app.post("/orders", status_code=201)
def create_order(body: OrderIn):
    order_id = uuid.uuid4().hex[:12]

    try:
        reservation = call(
            "POST",
            f"{INVENTORY_URL}/inventory/reserve",
            json={"order_id": order_id, **body.model_dump()},
            dependency="inventory",
            timeout=INVENTORY_TIMEOUT,
        )
    except ServiceError:
        # A timed-out reservation may still have been applied; release it to be safe.
        _compensate_in_background(order_id, void_payment=False)
        raise
    if reservation.status_code != 200:
        detail = reservation.json()
        raise ServiceError(
            reservation.status_code,
            detail.get("error", "ReservationFailed"),
            detail.get("message", f"inventory returned {reservation.status_code}"),
        )

    amount = round(reservation.json()["unit_price"] * body.quantity, 2)
    try:
        payment = call(
            "POST",
            f"{PAYMENT_URL}/payments",
            json={"order_id": order_id, "amount": amount},
            dependency="payment",
            timeout=PAYMENT_TIMEOUT,
        )
        if payment.status_code not in (200, 201):
            raise ServiceError(502, "PaymentRejected", f"payment returned {payment.status_code}")
    except ServiceError:
        _compensate_in_background(order_id, void_payment=True)
        raise

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


def _compensate_in_background(order_id: str, *, void_payment: bool) -> None:
    # Copy this request's context so compensation calls carry the same trace id.
    _compensations.submit(contextvars.copy_context().run, compensate, order_id, void_payment)


def compensate(order_id: str, void_payment: bool) -> None:
    """Undo a failed order: void its payment (if one was attempted), then release its stock."""
    voided = not void_payment or _with_retries(
        order_id,
        "void_payment",
        lambda: call(
            "POST",
            f"{PAYMENT_URL}/payments/void",
            json={"order_id": order_id},
            dependency="payment",
            timeout=PAYMENT_TIMEOUT,
        ),
    )
    released = _with_retries(
        order_id,
        "release_stock",
        lambda: call(
            "POST",
            f"{INVENTORY_URL}/inventory/release",
            json={"order_id": order_id},
            dependency="inventory",
            timeout=INVENTORY_TIMEOUT,
        ),
    )
    if voided and released:
        log.info("order compensated", extra={"fields": {"order_id": order_id}})


def _with_retries(order_id: str, step: str, action) -> bool:
    """Run action until it returns HTTP 200. Retries timeouts and 5xx; a 4xx won't improve."""
    for attempt, delay in enumerate((0, *COMPENSATION_BACKOFF_S), start=1):
        time.sleep(delay)
        try:
            response = action()
        except ServiceError as exc:
            last_error = exc
            log.warning(
                "compensation step failed",
                extra={"fields": {"order_id": order_id, "step": step, "attempt": attempt}},
            )
            continue
        if response.status_code == 200:
            return True
        log.error(
            "compensation step rejected",
            extra={
                "fields": {"order_id": order_id, "step": step, "status_code": response.status_code}
            },
        )
        return False
    log.error(
        "compensation gave up",
        extra={
            "fields": {
                "order_id": order_id,
                "step": step,
                "error_type": last_error.error_type,
                "error_message": last_error.message,
            }
        },
    )
    return False


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    order = _orders.get(order_id)
    if order is None:
        raise ServiceError(404, "OrderNotFound", f"no order {order_id}")
    return order
