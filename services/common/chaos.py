"""Failure injection ("chaos") so incidents can be reproduced on demand.

Every service exposes:
    GET    /chaos   current settings
    POST   /chaos   change settings, e.g. {"latency_ms": 3000} or {"error_rate": 0.4}
    DELETE /chaos   back to normal

Supported failures:
    latency_ms   extra delay added to every request
    error_rate   fraction of requests (0-1) that fail with HTTP 500
    db_delay_s   (payment only) every query first waits this long inside Postgres,
                 holding its pooled connection, so the pool genuinely runs out
    crash        the process exits immediately (restart with `docker compose start <service>`)
"""

import asyncio
import os
import random
import time
from dataclasses import asdict, dataclass

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common.context import RequestContext
from common.errors import ServiceError
from common.logs import get_logger

EXEMPT_PATHS = {"/health", "/chaos"}  # chaos never blocks its own controls or health checks


@dataclass
class ChaosState:
    latency_ms: int = 0
    error_rate: float = 0.0
    db_delay_s: float = 0.0
    intermittent_every: int = 0
    cpu_ms: int = 0


state = ChaosState()
_request_number = 0


class ChaosIn(BaseModel):
    latency_ms: int | None = Field(default=None, ge=0, le=30_000)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    db_delay_s: float | None = Field(default=None, ge=0, le=30)
    intermittent_every: int | None = Field(default=None, ge=0, le=100)
    cpu_ms: int | None = Field(default=None, ge=0, le=5000)
    crash: bool = False


async def apply(ctx: RequestContext) -> JSONResponse | None:
    """Apply injected latency/errors to a request. Returns a 500 response if it should fail."""
    global _request_number
    _request_number += 1
    if state.cpu_ms:
        await asyncio.to_thread(_burn_cpu, state.cpu_ms)
    if state.latency_ms:
        await asyncio.sleep(state.latency_ms / 1000)
    intermittent = state.intermittent_every and _request_number % state.intermittent_every == 0
    if intermittent or (state.error_rate and random.random() < state.error_rate):
        ctx.error_type = "InternalServerError"
        ctx.error_message = "unexpected error while processing request"
        return JSONResponse(
            status_code=500, content={"error": ctx.error_type, "message": ctx.error_message}
        )
    return None


def _burn_cpu(milliseconds: int) -> None:
    """Bounded CPU work off the event loop; reset is effective on the next request."""
    until = time.perf_counter() + milliseconds / 1000
    value = 1
    while time.perf_counter() < until:
        value = (value * 1664525 + 1013904223) & 0xFFFFFFFF


def _exit_process() -> None:
    os._exit(1)


def router(supports_db_delay: bool) -> APIRouter:
    r = APIRouter()
    log = get_logger()

    @r.get("/chaos")
    def get_chaos():
        return asdict(state)

    @r.post("/chaos")
    def set_chaos(body: ChaosIn, background: BackgroundTasks):
        global _request_number
        if body.db_delay_s is not None and not supports_db_delay:
            raise ServiceError(400, "UnsupportedChaos", "this service has no database")
        for field in ("latency_ms", "error_rate", "db_delay_s", "intermittent_every", "cpu_ms"):
            value = getattr(body, field)
            if value is not None:
                setattr(state, field, value)
                if field == "intermittent_every":
                    _request_number = 0
        log.warning("chaos updated", extra={"fields": {"chaos": asdict(state)}})
        if body.crash:
            log.warning("chaos crash requested")
            background.add_task(_exit_process)  # runs after the response is sent
        return asdict(state)

    @r.delete("/chaos")
    def reset_chaos():
        global _request_number
        state.latency_ms, state.error_rate, state.db_delay_s = 0, 0.0, 0.0
        state.intermittent_every, state.cpu_ms, _request_number = 0, 0, 0
        log.warning("chaos updated", extra={"fields": {"chaos": asdict(state)}})
        return asdict(state)

    return r
