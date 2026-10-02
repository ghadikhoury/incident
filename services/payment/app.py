"""Payment service: records payments in Postgres through a small connection pool.

The pool is deliberately small (POOL_SIZE) so that slow queries can genuinely
exhaust it, which is one of the failure scenarios Incident must detect.
"""

import os
from contextlib import asynccontextmanager

import psycopg
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
    yield
    pool.close()


app = create_app("payment", lifespan=lifespan)


class PaymentIn(BaseModel):
    order_id: str
    amount: float = Field(gt=0)


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
def create_payment(body: PaymentIn):
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO payments (order_id, amount, status) VALUES (%s, %s, 'captured') "
                "RETURNING id, created_at",
                (body.order_id, body.amount),
            ).fetchone()
    except (PoolTimeout, psycopg.Error) as exc:
        raise _db_error(exc) from exc
    return {
        "payment_id": row[0],
        "order_id": body.order_id,
        "amount": body.amount,
        "status": "captured",
        "created_at": row[1].isoformat(),
    }


@app.get("/payments/{payment_id}")
def get_payment(payment_id: int):
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT id, order_id, amount, status, created_at FROM payments WHERE id = %s",
                (payment_id,),
            ).fetchone()
    except (PoolTimeout, psycopg.Error) as exc:
        raise _db_error(exc) from exc
    if row is None:
        raise ServiceError(404, "PaymentNotFound", f"no payment {payment_id}")
    return {
        "payment_id": row[0],
        "order_id": row[1],
        "amount": float(row[2]),
        "status": row[3],
        "created_at": row[4].isoformat(),
    }
