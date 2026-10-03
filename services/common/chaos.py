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
    intermittent_on_s / intermittent_off_s
                 all requests fail during the on phase, then recover during the off phase
    cpu_ms       bounded CPU work per request, run in a worker thread
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
    intermittent_on_s: int = 0
    intermittent_off_s: int = 0
    cpu_ms: int = 0


state = ChaosState()
_intermittent_started = 0.0


class ChaosIn(BaseModel):
    latency_ms: int | None = Field(default=None, ge=0, le=30_000)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    db_delay_s: float | None = Field(default=None, ge=0, le=30)
    intermittent_on_s: int | None = Field(default=None, ge=0, le=300)
    intermittent_off_s: int | None = Field(default=None, ge=0, le=300)
    cpu_ms: int | None = Field(default=None, ge=0, le=5000)
    crash: bool = False


async def apply(ctx: RequestContext) -> JSONResponse | None:
    """Apply injected latency/errors to a request. Returns a 500 response if it should fail."""
    if state.cpu_ms:
        await asyncio.to_thread(_burn_cpu, state.cpu_ms)
    if state.latency_ms:
        await asyncio.sleep(state.latency_ms / 1000)
    cycle = state.intermittent_on_s + state.intermittent_off_s
    intermittent = bool(
        state.intermittent_on_s
        and cycle
        and (time.monotonic() - _intermittent_started) % cycle < state.intermittent_on_s
    )
    if intermittent or (state.error_rate and random.random() < state.error_rate):
        ctx.error_type = "InternalServerError"
        ctx.error_message = "unexpected error while processing request"
        return JSONResponse(
            status_code=500, content={"error": ctx.error_type, "message": ctx.error_message}
        )
    return None


def _burn_cpu(milliseconds: int) -> None:
    """Bounded CPU work that stops promptly when the injected setting is cleared."""
    until = time.perf_counter() + milliseconds / 1000
    value = 1
    while state.cpu_ms == milliseconds and time.perf_counter() < until:
        value = (value * 1664525 + 1013904223) & 0xFFFFFFFF


def _exit_process() -> None:
    os._exit(1)


def router(supports_db_delay: bool) -> APIRouter:
    r = APIRouter()
    log = get_logger()

    @r.get("/chaos")
    async def get_chaos():
        return asdict(state)

    @r.post("/chaos")
    async def set_chaos(body: ChaosIn, background: BackgroundTasks):
        global _intermittent_started
        if body.db_delay_s is not None and not supports_db_delay:
            raise ServiceError(400, "UnsupportedChaos", "this service has no database")
        for field in (
            "latency_ms",
            "error_rate",
            "db_delay_s",
            "intermittent_on_s",
            "intermittent_off_s",
            "cpu_ms",
        ):
            value = getattr(body, field)
            if value is not None:
                setattr(state, field, value)
                if field in {"intermittent_on_s", "intermittent_off_s"}:
                    _intermittent_started = time.monotonic()
        log.warning("chaos updated", extra={"fields": {"chaos": asdict(state)}})
        if body.crash:
            log.warning("chaos crash requested")
            background.add_task(_exit_process)  # runs after the response is sent
        return asdict(state)

    @r.delete("/chaos")
    async def reset_chaos():
        global _intermittent_started
        state.latency_ms, state.error_rate, state.db_delay_s = 0, 0.0, 0.0
        state.intermittent_on_s, state.intermittent_off_s = 0, 0
        state.cpu_ms, _intermittent_started = 0, 0.0
        log.warning("chaos updated", extra={"fields": {"chaos": asdict(state)}})
        return asdict(state)

    return r
