"""Payment service: records payments in Postgres through a small connection pool.

The pool is deliberately small (POOL_SIZE) so that slow queries can genuinely
exhaust it, which is one of the failure scenarios Incident must detect.

Payments are idempotent per order_id (unique in the table): repeating a request
returns the existing payment instead of charging twice, and voiding an order
leaves a "voided" row so a slow, late-arriving charge for it can't succeed.
"""

import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import Response
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, Field

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
                   status TEXT NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS payments_order_id_key ON payments (order_id)"
        )
    yield
    pool.close()


app = create_app("payment", lifespan=lifespan)


class PaymentIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0)


class VoidIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)


COLUMNS = "id, order_id, amount, status, created_at"


def _payment(row) -> dict:
    return {
        "payment_id": row[0],
        "order_id": row[1],
        "amount": float(row[2]),
        "status": row[3],
        "created_at": row[4].isoformat(),
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
            row = conn.execute(
                "INSERT INTO payments (order_id, amount, status) VALUES (%s, %s, 'captured') "
                f"ON CONFLICT (order_id) DO NOTHING RETURNING {COLUMNS}",
                (body.order_id, body.amount),
            ).fetchone()
            if row is None:  # already exists: this is a retry (or the order was voided)
                row = conn.execute(
                    f"SELECT {COLUMNS} FROM payments WHERE order_id = %s", (body.order_id,)
                ).fetchone()
                response.status_code = 200
    except (PoolTimeout, psycopg.Error) as exc:
        raise _db_error(exc) from exc
    payment = _payment(row)
    if payment["status"] == "voided":
        raise ServiceError(409, "PaymentVoided", f"order {body.order_id} was cancelled")
    if payment["amount"] != round(body.amount, 2):
        raise ServiceError(
            409, "IdempotencyConflict", f"order {body.order_id} was charged a different amount"
        )
    return payment


@app.post("/payments/void")
def void_payment(body: VoidIn):
    """Cancel an order's payment. Safe to retry, and safe to call before the charge lands."""
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO payments (order_id, amount, status) VALUES (%s, 0, 'voided') "
                "ON CONFLICT (order_id) DO UPDATE SET status = 'voided' "
                f"RETURNING {COLUMNS}",
                (body.order_id,),
            ).fetchone()
    except (PoolTimeout, psycopg.Error) as exc:
        raise _db_error(exc) from exc
    return _payment(row)


@app.get("/payments/{payment_id}")
def get_payment(payment_id: int):
    try:
        with pool.connection() as conn:
            row = conn.execute(
                f"SELECT {COLUMNS} FROM payments WHERE id = %s", (payment_id,)
            ).fetchone()
    except (PoolTimeout, psycopg.Error) as exc:
        raise _db_error(exc) from exc
    if row is None:
        raise ServiceError(404, "PaymentNotFound", f"no payment {payment_id}")
    return _payment(row)
