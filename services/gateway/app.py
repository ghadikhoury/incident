"""API gateway: the single public entry point. Forwards requests to the order service."""

import os
from typing import Annotated

from fastapi import Body
from fastapi.responses import JSONResponse

from common.app import create_app
from common.http import call

ORDER_URL = os.getenv("ORDER_URL", "http://localhost:8091")
ORDER_TIMEOUT = float(os.getenv("ORDER_TIMEOUT", "5.0"))

app = create_app("gateway")


@app.get("/health")
def health():
    return {"status": "ok", "service": "gateway"}


def _forward(method: str, path: str, **kwargs) -> JSONResponse:
    response = call(
        method, f"{ORDER_URL}{path}", dependency="order", timeout=ORDER_TIMEOUT, **kwargs
    )
    return JSONResponse(status_code=response.status_code, content=response.json())


@app.post("/orders")
def create_order(payload: Annotated[dict, Body()]):
    return _forward("POST", "/orders", json=payload)


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    return _forward("GET", f"/orders/{order_id}")
