from fastapi.testclient import TestClient

from inventory.app import app

client = TestClient(app)


def test_quote_returns_unit_price_and_total():
    response = client.post("/inventory/quote", json={"item_id": "sku-2", "quantity": 3})
    assert response.status_code == 200
    assert response.json() == {
        "item_id": "sku-2",
        "quantity": 3,
        "unit_price": 24.50,
        "total": 73.50,
    }


def test_quoting_changes_nothing():
    before = client.get("/inventory/sku-2").json()
    client.post("/inventory/quote", json={"item_id": "sku-2", "quantity": 5})
    assert client.get("/inventory/sku-2").json() == before


def test_unknown_item_is_404():
    for response in (
        client.post("/inventory/quote", json={"item_id": "nope", "quantity": 1}),
        client.get("/inventory/nope"),
    ):
        assert response.status_code == 404
        assert response.json()["error"] == "ItemNotFound"


def test_invalid_quantity_is_rejected():
    response = client.post("/inventory/quote", json={"item_id": "sku-1", "quantity": 0})
    assert response.status_code == 422
