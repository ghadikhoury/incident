"""Inventory service: item prices and stock reservations (in memory)."""

import threading

from pydantic import BaseModel, Field

from common.app import create_app
from common.errors import ServiceError

app = create_app("inventory")

STARTING_STOCK = 1_000_000  # effectively unlimited so the simulation never runs out
PRICES = {"sku-1": 9.99, "sku-2": 24.50, "sku-3": 4.25, "sku-4": 120.00, "sku-5": 59.90}

_stock = dict.fromkeys(PRICES, STARTING_STOCK)
_lock = threading.Lock()


class ReserveIn(BaseModel):
    item_id: str
    quantity: int = Field(gt=0, le=100)


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
        if _stock[body.item_id] < body.quantity:
            raise ServiceError(409, "OutOfStock", f"not enough stock for {body.item_id}")
        _stock[body.item_id] -= body.quantity
    return {
        "item_id": body.item_id,
        "quantity": body.quantity,
        "unit_price": PRICES[body.item_id],
    }
