from fastapi.testclient import TestClient

from inventory.app import app

client = TestClient(app)


def test_reserve_returns_price_and_reduces_stock():
    before = client.get("/inventory/sku-2").json()["stock"]
    response = client.post("/inventory/reserve", json={"item_id": "sku-2", "quantity": 2})
    assert response.status_code == 200
    assert response.json()["unit_price"] == 24.50
    assert client.get("/inventory/sku-2").json()["stock"] == before - 2


def test_unknown_item_is_404():
    response = client.post("/inventory/reserve", json={"item_id": "nope", "quantity": 1})
    assert response.status_code == 404
    assert response.json()["error"] == "ItemNotFound"
