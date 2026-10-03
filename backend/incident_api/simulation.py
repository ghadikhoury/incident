"""Demo controls: inject and clear failures in the simulated services via their /chaos API."""

from typing import Literal

import httpx
from pydantic import BaseModel

from incident_api.config import MonitoredService

FailureType = Literal["db_slow", "latency", "error_rate", "crash", "intermittent", "cpu"]

# What each failure sends to the service's /chaos endpoint.
FAILURES: dict[str, dict] = {
    "db_slow": {"db_delay_s": 3.0},
    "latency": {"latency_ms": 3000},
    "error_rate": {"error_rate": 0.4},
    "crash": {"crash": True},
    "intermittent": {"intermittent_on_s": 120, "intermittent_off_s": 60},
    "cpu": {"cpu_ms": 2300},
}
DB_ONLY_FAILURES = {"db_slow"}
DB_SERVICES = {"payment"}


class FailureRequest(BaseModel):
    service: str
    failure: FailureType


class RecoverRequest(BaseModel):
    service: str


class SimulationError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def inject(client: httpx.AsyncClient, service: MonitoredService, failure: str) -> dict:
    if failure in DB_ONLY_FAILURES and service.name not in DB_SERVICES:
        raise SimulationError(400, f"{failure} only applies to services with a database")
    return await _chaos_call(client, "POST", service, json=FAILURES[failure])


async def recover(client: httpx.AsyncClient, service: MonitoredService) -> dict:
    return await _chaos_call(client, "DELETE", service)


async def _chaos_call(
    client: httpx.AsyncClient, method: str, service: MonitoredService, **kwargs
) -> dict:
    try:
        response = await client.request(method, f"{service.url}/chaos", timeout=5, **kwargs)
    except httpx.HTTPError as exc:
        raise SimulationError(
            409,
            f"{service.name} is not reachable. If it crashed, restart it with "
            f"`docker compose start {service.name}`.",
        ) from exc
    if response.status_code != 200:
        raise SimulationError(502, f"{service.name} rejected the request: {response.text}")
    return response.json()
