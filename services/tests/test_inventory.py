import uuid

from fastapi.testclient import TestClient

from inventory.app import app

client = TestClient(app)


def stock(item_id: str) -> int:
    return client.get(f"/inventory/{item_id}").json()["stock"]


def reserve(order_id: str, item_id: str = "sku-2", quantity: int = 2):
    return client.post(
        "/inventory/reserve", json={"order_id": order_id, "item_id": item_id, "quantity": quantity}
    )


def new_order_id() -> str:
    return uuid.uuid4().hex


def test_reserve_returns_price_and_reduces_stock():
    before = stock("sku-2")
    response = reserve(new_order_id())
    assert response.status_code == 200
    assert response.json()["unit_price"] == 24.50
    assert stock("sku-2") == before - 2


def test_reserve_is_idempotent_per_order():
    order_id = new_order_id()
    before = stock("sku-2")
    reserve(order_id)
    reserve(order_id)
    assert stock("sku-2") == before - 2


def test_release_returns_stock_exactly_once():
    order_id = new_order_id()
    before = stock("sku-3")
    reserve(order_id, "sku-3", 5)
    first = client.post("/inventory/release", json={"order_id": order_id})
    second = client.post("/inventory/release", json={"order_id": order_id})
    assert first.json()["released"] is True
    assert second.json()["released"] is False
    assert stock("sku-3") == before


def test_release_of_unknown_order_is_a_no_op():
    response = client.post("/inventory/release", json={"order_id": new_order_id()})
    assert response.status_code == 200
    assert response.json()["released"] is False


def test_unknown_item_is_404():
    response = reserve(new_order_id(), item_id="nope", quantity=1)
    assert response.status_code == 404
    assert response.json()["error"] == "ItemNotFound"
