"""Polls every monitored service's /health (and /chaos) and keeps the latest snapshot."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Literal

import httpx
from pydantic import BaseModel

from incident_api.config import MonitoredService
from incident_api.metrics import emit_health_checks
from incident_api.store import now_iso

log = logging.getLogger(__name__)

HealthStatus = Literal["healthy", "unhealthy", "down", "unknown"]


class ServiceHealth(BaseModel):
    name: str
    display_name: str
    status: HealthStatus
    depends_on: list[str]
    error: str | None = None
    response_ms: float | None = None
    chaos: dict | None = None
    checked_at: str | None = None

    def same_state_as(self, other: "ServiceHealth | None") -> bool:
        """Ignores timing fields, so we only broadcast real changes."""
        keys = {"status", "error", "chaos"}
        return other is not None and self.model_dump(include=keys) == other.model_dump(include=keys)


class HealthMonitor:
    def __init__(
        self,
        services: tuple[MonitoredService, ...],
        client: httpx.AsyncClient,
        timeout_s: float,
        on_change: Callable[[list[ServiceHealth]], Awaitable[None]],
    ):
        self.services = services
        self.client = client
        self.timeout_s = timeout_s
        self.on_change = on_change
        self.snapshot = {
            s.name: ServiceHealth(
                name=s.name,
                display_name=s.display_name,
                status="unknown",
                depends_on=list(s.depends_on),
            )
            for s in services
        }

    def current(self) -> list[ServiceHealth]:
        return list(self.snapshot.values())

    async def poll_once(self) -> None:
        results = await asyncio.gather(*(self._check(s) for s in self.services))
        emit_health_checks(results)
        changed = any(not r.same_state_as(self.snapshot.get(r.name)) for r in results)
        self.snapshot = {r.name: r for r in results}
        if changed:
            await self.on_change(self.current())

    async def run(self, interval_s: float) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("health poll failed")
            await asyncio.sleep(interval_s)

    async def _check(self, service: MonitoredService) -> ServiceHealth:
        result = ServiceHealth(
            name=service.name,
            display_name=service.display_name,
            status="down",
            depends_on=list(service.depends_on),
            checked_at=now_iso(),
        )
        start = time.perf_counter()
        try:
            response = await self.client.get(f"{service.url}/health", timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            result.error = f"unreachable: {type(exc).__name__}"
            return result
        result.response_ms = round((time.perf_counter() - start) * 1000, 1)
        if response.status_code == 200:
            result.status = "healthy"
        else:
            result.status = "unhealthy"
            result.error = _error_message(response)
        try:
            chaos = await self.client.get(f"{service.url}/chaos", timeout=self.timeout_s)
            result.chaos = chaos.json() if chaos.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            result.chaos = None
        return result


def _error_message(response: httpx.Response) -> str:
    try:
        return response.json().get("message") or f"HTTP {response.status_code}"
    except ValueError:
        return f"HTTP {response.status_code}"
