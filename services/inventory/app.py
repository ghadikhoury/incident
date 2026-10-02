"""Inventory service: item prices and stock reservations (in memory).

Reservations are keyed by order_id so both reserving and releasing are idempotent:
a retried call never takes or returns stock twice.
"""

import threading
from collections import OrderedDict

from pydantic import BaseModel, Field

from common.app import create_app
from common.errors import ServiceError

app = create_app("inventory")

STARTING_STOCK = 1_000_000  # effectively unlimited so the simulation never runs out
PRICES = {"sku-1": 9.99, "sku-2": 24.50, "sku-3": 4.25, "sku-4": 120.00, "sku-5": 59.90}
MAX_TRACKED_RESERVATIONS = 50_000  # oldest are forgotten; old orders are never released

_stock = dict.fromkeys(PRICES, STARTING_STOCK)
_reservations: OrderedDict[str, tuple[str, int]] = OrderedDict()  # order_id -> (item, qty)
_lock = threading.Lock()


class ReserveIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    item_id: str
    quantity: int = Field(gt=0, le=100)


class ReleaseIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)


@app.get("/health")
def health():
    return {"status": "ok", "service": "inventory"}


@app.get("/inventory/{item_id}")
def get_item(item_id: str):
    if item_id not in PRICES:
        raise ServiceError(404, "ItemNotFound", f"unknown item {item_id}")
    return {"item_id": item_id, "price": PRICES[item_id], "stock": _stock[item_id]}


@app.post("/inventory/reserve")
def reserve(body: ReserveIn):
    if body.item_id not in PRICES:
        raise ServiceError(404, "ItemNotFound", f"unknown item {body.item_id}")
    with _lock:
        if body.order_id not in _reservations:
            if _stock[body.item_id] < body.quantity:
                raise ServiceError(409, "OutOfStock", f"not enough stock for {body.item_id}")
            _stock[body.item_id] -= body.quantity
            _reservations[body.order_id] = (body.item_id, body.quantity)
            if len(_reservations) > MAX_TRACKED_RESERVATIONS:
                _reservations.popitem(last=False)
        item_id, quantity = _reservations[body.order_id]
    return {
        "order_id": body.order_id,
        "item_id": item_id,
        "quantity": quantity,
        "unit_price": PRICES[item_id],
    }


@app.post("/inventory/release")
def release(body: ReleaseIn):
    """Return a failed order's stock. Safe to retry: only the first call has an effect."""
    with _lock:
        reservation = _reservations.pop(body.order_id, None)
        if reservation:
            item_id, quantity = reservation
            _stock[item_id] += quantity
    return {"order_id": body.order_id, "released": reservation is not None}
