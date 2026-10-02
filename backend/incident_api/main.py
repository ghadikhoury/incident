"""Incident backend API.

Run locally (from backend/):  uvicorn --factory incident_api.main:create_app --port 8000
"""

import asyncio
import contextlib
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from incident_api import config, simulation
from incident_api.health import HealthMonitor, ServiceHealth
from incident_api.hub import Hub
from incident_api.models import (
    Incident,
    IncidentAction,
    IncidentCreate,
    IncidentDetail,
    IncidentUpdate,
    Status,
    TimelineEvent,
)
from incident_api.store import Conflict, IncidentStore, NotFound


def create_app(
    *,
    store: IncidentStore | None = None,
    http_client: httpx.AsyncClient | None = None,
    services: tuple[config.MonitoredService, ...] = config.SERVICES,
    health_interval_s: float = config.HEALTH_INTERVAL_S,
) -> FastAPI:
    store = store or IncidentStore.from_config()
    client = http_client or httpx.AsyncClient()
    hub = Hub()
    services_by_name = {s.name: s for s in services}

    async def broadcast_services(snapshot: list[ServiceHealth]) -> None:
        await hub.broadcast({"type": "services", "data": [s.model_dump() for s in snapshot]})

    async def broadcast_incident(incident: Incident) -> None:
        await hub.broadcast({"type": "incident", "data": incident.model_dump(mode="json")})

    monitor = HealthMonitor(services, client, config.HEALTH_TIMEOUT_S, broadcast_services)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(monitor.run(health_interval_s))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if http_client is None:
            await client.aclose()

    app = FastAPI(title="Incident", lifespan=lifespan)

    async def change_incident(
        incident_id: str,
        changes: dict,
        events: list[tuple[str, str]],
        actor: str | None,
        require_status: Status | None = None,
    ) -> Incident:
        try:
            incident = await run_in_threadpool(
                store.update, incident_id, changes, events, actor, require_status=require_status
            )
        except NotFound as exc:
            raise HTTPException(404, f"incident {incident_id} not found") from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        await broadcast_incident(incident)
        return incident

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/services")
    def list_services() -> list[ServiceHealth]:
        return monitor.current()

    @app.get("/api/incidents")
    async def list_incidents(active: bool = False) -> list[Incident]:
        return await run_in_threadpool(store.list_incidents, active)

    @app.post("/api/incidents", status_code=201)
    async def create_incident(body: IncidentCreate) -> Incident:
        incident = await run_in_threadpool(store.create, body)
        await broadcast_incident(incident)
        return incident

    @app.get("/api/incidents/{incident_id}")
    async def get_incident(incident_id: str) -> IncidentDetail:
        incident = await run_in_threadpool(store.get, incident_id)
        if incident is None:
            raise HTTPException(404, f"incident {incident_id} not found")
        timeline = await run_in_threadpool(store.timeline, incident_id)
        return IncidentDetail(**incident.model_dump(), timeline=timeline)

    @app.get("/api/incidents/{incident_id}/timeline")
    async def get_timeline(incident_id: str) -> list[TimelineEvent]:
        if await run_in_threadpool(store.get, incident_id) is None:
            raise HTTPException(404, f"incident {incident_id} not found")
        return await run_in_threadpool(store.timeline, incident_id)

    @app.patch("/api/incidents/{incident_id}")
    async def update_incident(incident_id: str, body: IncidentUpdate) -> Incident:
        changes = body.model_dump(exclude_unset=True, exclude={"actor"})
        if not changes:
            raise HTTPException(400, "nothing to update")
        if changes.get("status") == Status.OPEN:
            raise HTTPException(400, "an incident cannot be moved back to OPEN")
        for field in ("title", "severity", "status"):
            if field in changes and changes[field] is None:
                raise HTTPException(400, f"{field} cannot be empty")
        return await change_incident(incident_id, changes, _describe(changes), body.actor)

    @app.post("/api/incidents/{incident_id}/acknowledge")
    async def acknowledge(incident_id: str, body: IncidentAction) -> Incident:
        message = _with_note("Acknowledged", body.note)
        return await change_incident(
            incident_id,
            {"status": Status.ACKNOWLEDGED},
            [("status", message)],
            body.actor,
            require_status=Status.OPEN,
        )

    @app.post("/api/incidents/{incident_id}/resolve")
    async def resolve(incident_id: str, body: IncidentAction) -> Incident:
        message = _with_note("Resolved", body.note)
        return await change_incident(
            incident_id, {"status": Status.RESOLVED}, [("status", message)], body.actor
        )

    @app.post("/api/simulation/failure")
    async def inject_failure(body: simulation.FailureRequest) -> dict:
        service = _service_or_404(services_by_name, body.service)
        try:
            chaos = await simulation.inject(client, service, body.failure)
        except simulation.SimulationError as exc:
            raise HTTPException(exc.status_code, exc.message) from exc
        await monitor.poll_once()  # show the change on dashboards right away
        return {"service": service.name, "chaos": chaos}

    @app.post("/api/simulation/recover")
    async def recover(body: simulation.RecoverRequest) -> dict:
        service = _service_or_404(services_by_name, body.service)
        try:
            chaos = await simulation.recover(client, service)
        except simulation.SimulationError as exc:
            raise HTTPException(exc.status_code, exc.message) from exc
        await monitor.poll_once()
        return {"service": service.name, "chaos": chaos}

    @app.websocket("/api/ws")
    async def websocket(websocket: WebSocket):
        await hub.connect(websocket)
        try:
            snapshot = [s.model_dump() for s in monitor.current()]
            await hub.send(websocket, {"type": "services", "data": snapshot})
            while True:
                await websocket.receive_text()  # clients don't send anything; wait for close
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(websocket)

    return app


def _service_or_404(services_by_name: dict, name: str) -> config.MonitoredService:
    if name not in services_by_name:
        raise HTTPException(404, f"unknown service {name}")
    return services_by_name[name]


def _with_note(message: str, note: str | None) -> str:
    return f"{message}: {note}" if note else message


def _describe(changes: dict) -> list[tuple[str, str]]:
    """Timeline entries for a set of field changes."""
    events = []
    if "status" in changes:
        events.append(("status", f"Status changed to {changes['status']}"))
    if "severity" in changes:
        events.append(("severity", f"Severity set to {changes['severity']}"))
    if "assigned_to" in changes:
        owner = changes["assigned_to"]
        events.append(("assignment", f"Assigned to {owner}" if owner else "Unassigned"))
    if "title" in changes:
        events.append(("title", f'Title changed to "{changes["title"]}"'))
    return events
