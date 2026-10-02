"""Payment service: records payments in Postgres through a small connection pool.

The pool is deliberately small (POOL_SIZE) so that slow queries can genuinely
exhaust it, which is one of the failure scenarios Incident must detect.

Payments are idempotent per order_id (unique in the table): repeating a request
returns the existing payment instead of charging twice. A captured payment is the
record that an order exists, which is how the order service resolves requests whose
outcome was unknown (e.g. it timed out while the charge was still being written).
"""

import os
from contextlib import asynccontextmanager
from typing import Literal

import psycopg
from fastapi import Response
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, Field, model_validator

from common import chaos
from common.app import create_app
from common.errors import ServiceError

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://incident:incident@localhost:5432/payments")
POOL_SIZE = int(os.getenv("POOL_SIZE", "5"))
POOL_TIMEOUT = float(os.getenv("POOL_TIMEOUT", "2.0"))  # seconds to wait for a free connection

pool = ConnectionPool(
    DATABASE_URL, min_size=1, max_size=POOL_SIZE, timeout=POOL_TIMEOUT, open=False
)


@asynccontextmanager
async def lifespan(app):
    pool.open(wait=True, timeout=30)
    with pool.connection() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS payments (
                   id SERIAL PRIMARY KEY,
                   order_id TEXT NOT NULL,
                   amount NUMERIC(10, 2) NOT NULL,
                   item_id TEXT,
                   quantity INTEGER,
                   status TEXT NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
        # Existing installations predate order identity in the payment record.
        conn.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS item_id TEXT")
        conn.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS quantity INTEGER")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS payments_order_id_key ON payments (order_id)"
        )
    yield
    pool.close()


app = create_app("payment", supports_db_chaos=True, lifespan=lifespan)


def _injected_db_delay(conn) -> None:
    """Chaos: make the query slow inside Postgres, so it holds its pooled connection."""
    if chaos.state.db_delay_s:
        conn.execute("SELECT pg_sleep(%s)", (chaos.state.db_delay_s,))


class PaymentIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0)
    item_id: str | None = Field(default=None, min_length=1)
    quantity: int | None = Field(default=None, gt=0, le=100)

    @model_validator(mode="after")
    def complete_order_identity(self):
        if (self.item_id is None) != (self.quantity is None):
            raise ValueError("item_id and quantity must be supplied together")
        return self


COLUMNS = "id, order_id, amount, item_id, quantity, status, created_at"


def _payment(row) -> dict:
    return {
        "payment_id": row[0],
        "order_id": row[1],
        "amount": float(row[2]),
        "item_id": row[3],
        "quantity": row[4],
        "status": row[5],
        "created_at": row[6].isoformat(),
    }


def _db_error(exc: Exception) -> ServiceError:
    if isinstance(exc, PoolTimeout):
        return ServiceError(503, "PoolTimeout", f"database connection pool exhausted: {exc}")
    return ServiceError(503, "DatabaseError", f"database error: {exc}")


@app.get("/health")
def health():
    try:
        with pool.connection(timeout=1.0) as conn:
            conn.execute("SELECT 1")
    except (PoolTimeout, psycopg.Error) as exc:
        raise _db_error(exc) from exc
    return {"status": "ok", "service": "payment"}


@app.post("/payments", status_code=201)
def create_payment(body: PaymentIn, response: Response):
    """Charge an order. 201 for a new payment, 200 if this order was already charged."""
    try:
        with pool.connection() as conn:
            _injected_db_delay(conn)
            row = conn.execute(
                "INSERT INTO payments (order_id, amount, item_id, quantity, status) "
                "VALUES (%s, %s, %s, %s, 'captured') "
                f"ON CONFLICT (order_id) DO NOTHING RETURNING {COLUMNS}",
                (body.order_id, body.amount, body.item_id, body.quantity),
            ).fetchone()
            if row is None:  # already charged: this is a retry
                row = conn.execute(
                    f"SELECT {COLUMNS} FROM payments WHERE order_id = %s", (body.order_id,)
                ).fetchone()
                response.status_code = 200
    except (PoolTimeout, psycopg.Error) as exc:
        raise _db_error(exc) from exc
    payment = _payment(row)
    if (
        payment["amount"] != round(body.amount, 2)
        or payment["item_id"] != body.item_id
        or payment["quantity"] != body.quantity
    ):
        raise ServiceError(
            409, "IdempotencyConflict", f"order {body.order_id} has different order details"
        )
    return payment


@app.get("/payments/{payment_id}")
def get_payment(payment_id: int):
    return _find("id", payment_id, f"no payment {payment_id}")


@app.get("/payments/by-order/{order_id}")
def get_payment_for_order(order_id: str):
    return _find("order_id", order_id, f"no payment for order {order_id}")


def _find(column: Literal["id", "order_id"], value, not_found: str) -> dict:
    # column is one of two fixed names (never user input); the value is a bound parameter.
    try:
        with pool.connection() as conn:
            _injected_db_delay(conn)
            row = conn.execute(
                f"SELECT {COLUMNS} FROM payments WHERE {column} = %s", (value,)
            ).fetchone()
    except (PoolTimeout, psycopg.Error) as exc:
        raise _db_error(exc) from exc
    if row is None:
        raise ServiceError(404, "PaymentNotFound", not_found)
    return _payment(row)
