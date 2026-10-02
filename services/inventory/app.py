"""Inventory service: item catalog and price quotes.

Deliberately stateless. Quoting an order changes nothing, so a failed or timed-out
order can never leave inventory in a wrong state. The service still matters to the
simulation as a dependency that can be slow or fail.
"""

from pydantic import BaseModel, Field

from common.app import create_app
from common.errors import ServiceError

app = create_app("inventory")

PRICES = {"sku-1": 9.99, "sku-2": 24.50, "sku-3": 4.25, "sku-4": 120.00, "sku-5": 59.90}


class QuoteIn(BaseModel):
    item_id: str
    quantity: int = Field(gt=0, le=100)


@app.get("/health")
def health():
    return {"status": "ok", "service": "inventory"}


def _price(item_id: str) -> float:
    if item_id not in PRICES:
        raise ServiceError(404, "ItemNotFound", f"unknown item {item_id}")
    return PRICES[item_id]


@app.get("/inventory/{item_id}")
def get_item(item_id: str):
    return {"item_id": item_id, "price": _price(item_id), "available": True}


@app.post("/inventory/quote")
def quote(body: QuoteIn):
    unit_price = _price(body.item_id)
    return {
        "item_id": body.item_id,
        "quantity": body.quantity,
        "unit_price": unit_price,
        "total": round(unit_price * body.quantity, 2),
    }
