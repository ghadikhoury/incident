import httpx
import pytest
from fastapi.testclient import TestClient

import order.app as order_app
from common.errors import ServiceError

client = TestClient(order_app.app)


@pytest.fixture
def downstream(monkeypatch):
    """Fake inventory/payment responses. Values are httpx.Response or ServiceError to raise."""
    responses = {
        "inventory": httpx.Response(200, json={"unit_price": 10.0}),
        "payment": httpx.Response(201, json={"payment_id": 7}),
    }

    def fake_call(method, url, *, dependency, timeout, **kwargs):
        result = responses[dependency]
        if isinstance(result, ServiceError):
            raise result
        return result

    monkeypatch.setattr(order_app, "call", fake_call)
    return responses


def test_create_order_happy_path(downstream):
    response = client.post("/orders", json={"item_id": "sku-1", "quantity": 3})
    assert response.status_code == 201
    body = response.json()
    assert body["amount"] == 30.0
    assert body["payment_id"] == 7
    assert client.get(f"/orders/{body['order_id']}").json() == body


def test_payment_timeout_propagates(downstream):
    downstream["payment"] = ServiceError(504, "DependencyTimeout", "payment did not respond")
    response = client.post("/orders", json={"item_id": "sku-1", "quantity": 1})
    assert response.status_code == 504
    assert response.json()["error"] == "DependencyTimeout"


def test_out_of_stock_is_passed_through(downstream):
    downstream["inventory"] = httpx.Response(
        409, json={"error": "OutOfStock", "message": "not enough stock"}
    )
    response = client.post("/orders", json={"item_id": "sku-1", "quantity": 1})
    assert response.status_code == 409
    assert response.json()["error"] == "OutOfStock"


def test_unknown_order_is_404():
    assert client.get("/orders/nope").status_code == 404


def test_invalid_quantity_is_rejected():
    assert client.post("/orders", json={"item_id": "sku-1", "quantity": 0}).status_code == 422
