"""Incident backend API.

Run locally (from backend/):  uvicorn --factory incident_api.main:create_app --port 8000
"""

import asyncio
import contextlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import boto3
import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from incident_api import config, simulation
from incident_api.dependency import GRAPH
from incident_api.diagnosis.engine import DiagnosisEngine
from incident_api.health import HealthMonitor, ServiceHealth
from incident_api.hub import Hub
from incident_api.local import LocalIncidentStore, LocalTelemetry
from incident_api.models import (
    Diagnosis,
    DiagnosisDecision,
    Incident,
    IncidentAction,
    IncidentCreate,
    IncidentDetail,
    IncidentUpdate,
    Status,
    TimelineEvent,
)
from incident_api.observability import IncidentTelemetry
from incident_api.store import Conflict, IncidentStore, NotFound

LOG = logging.getLogger(__name__)


def create_app(
    *,
    store: IncidentStore | LocalIncidentStore | None = None,
    http_client: httpx.AsyncClient | None = None,
    services: tuple[config.MonitoredService, ...] = config.SERVICES,
    health_interval_s: float = config.HEALTH_INTERVAL_S,
    queue_url: str | None = config.QUEUE_URL,
    sqs_client=None,
    diagnosis_engine: DiagnosisEngine | None = None,
    telemetry: IncidentTelemetry | None = None,
    diagnosis_delay_s: float | None = None,
    mode: str = config.INCIDENT_MODE,
    local_db_path: str = config.LOCAL_DB_PATH,
    local_failure_duration_s: float = config.LOCAL_FAILURE_DURATION_S,
) -> FastAPI:
    if mode not in {"local", "aws"}:
        raise ValueError(f"Unknown INCIDENT_MODE: {mode}; choose local or aws")
    local = mode == "local"
    if local_failure_duration_s < 0 or health_interval_s <= 0:
        raise ValueError("Health interval must be positive and failure duration nonnegative")
    if local and store is not None and not isinstance(store, LocalIncidentStore):
        raise ValueError("Local mode requires an isolated LocalIncidentStore")
    if not local and isinstance(store, LocalIncidentStore):
        raise ValueError("AWS mode cannot use the local incident store")
    owns_local_store = local and store is None
    store = store or (LocalIncidentStore(local_db_path) if local else IncidentStore.from_config())
    if local:
        queue_url = None  # never consume AWS updates into the local store
        telemetry = LocalTelemetry(store)
    if diagnosis_delay_s is None:
        diagnosis_delay_s = 6 if local else 100
    client = http_client or httpx.AsyncClient()
    hub = Hub()
    services_by_name = {s.name: s for s in services}
    diagnosis_tasks: dict[str, asyncio.Task] = {}
    stopping = False
    detection_checked_at: str | None = None
    detection_error = False

    async def broadcast_services(snapshot: list[ServiceHealth]) -> None:
        await hub.broadcast({"type": "services", "data": [s.model_dump() for s in snapshot]})

    async def broadcast_incident(incident: Incident) -> None:
        await hub.broadcast({"type": "incident", "data": incident.model_dump(mode="json")})

    async def observe_local(snapshot: list[ServiceHealth]) -> None:
        nonlocal detection_checked_at, detection_error
        try:
            updated = await run_in_threadpool(store.observe, snapshot, local_failure_duration_s)
            detection_checked_at = max(
                (s.checked_at for s in snapshot if s.checked_at), default=None
            )
            detection_error = False
            for incident in updated:
                await broadcast_incident(incident)
                schedule_diagnosis(incident.incident_id)
        except Exception:
            detection_error = True
            LOG.exception("Local incident detection failed")

    monitor = HealthMonitor(
        services,
        client,
        config.HEALTH_TIMEOUT_S,
        broadcast_services,
        observe_local if local else None,
    )

    async def consume_updates():
        sqs = sqs_client or boto3.Session(
            profile_name=config.AWS_PROFILE, region_name=config.AWS_REGION
        ).client("sqs")
        while True:
            try:
                response = await asyncio.to_thread(
                    sqs.receive_message,
                    QueueUrl=queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=10,
                )
                for message in response.get("Messages", []):
                    try:
                        incident_id = json.loads(message["Body"])["incident_id"]
                        incident = await run_in_threadpool(store.get, incident_id)
                        if incident is not None:
                            await broadcast_incident(incident)
                            schedule_diagnosis(incident.incident_id)
                        await asyncio.to_thread(
                            sqs.delete_message,
                            QueueUrl=queue_url,
                            ReceiptHandle=message["ReceiptHandle"],
                        )
                    except (ValueError, KeyError):
                        # Remove malformed messages; retry valid messages after AWS failures.
                        await asyncio.to_thread(
                            sqs.delete_message,
                            QueueUrl=queue_url,
                            ReceiptHandle=message["ReceiptHandle"],
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("SQS incident update failed; retrying")
                await asyncio.sleep(2)

    async def diagnose(incident_id: str, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)  # allow the alarm cascade to join the evidence set
            incident = await run_in_threadpool(store.get, incident_id)
            if incident is None or incident.status == Status.RESOLVED:
                return
            timeline = await run_in_threadpool(store.timeline, incident_id)
            if not any(event.kind == "evidence" for event in timeline):
                return
            claim = await run_in_threadpool(store.claim_diagnosis, incident_id)
            if claim is None:
                return
            await broadcast_incident(await run_in_threadpool(store.get, incident_id))
            try:
                engine = diagnosis_engine or await run_in_threadpool(
                    DiagnosisEngine.from_config, store if local else None
                )
                result = await run_in_threadpool(
                    engine.analyze, incident, timeline, set(services_by_name), claim
                )
            except Exception as exc:
                LOG.error("AI analysis failed for %s (%s)", incident_id, type(exc).__name__)
                missing = config.DIAGNOSIS_PROVIDER == "gemini" and not os.getenv("GEMINI_API_KEY")
                result = Diagnosis(
                    status="UNAVAILABLE",
                    claimed_at=claim,
                    unavailable_reason="missing_configuration" if missing else "provider_failure",
                )
            updated = await run_in_threadpool(store.finish_diagnosis, incident_id, claim, result)
            if updated:
                await broadcast_incident(updated)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Diagnosis worker failed for %s", incident_id)
        finally:
            diagnosis_tasks.pop(incident_id, None)

    def schedule_diagnosis(incident_id: str, delay_s: float = diagnosis_delay_s) -> None:
        if stopping:
            return
        if not local and not queue_url and diagnosis_engine is None:
            return
        existing = diagnosis_tasks.get(incident_id)
        if existing is not None:
            if delay_s == 0:
                existing.add_done_callback(lambda _task: schedule_diagnosis(incident_id, 0))
            return
        diagnosis_tasks[incident_id] = asyncio.create_task(diagnose(incident_id, delay_s))

    async def execute_recommendation(incident_id: str, action_id: str) -> Incident:
        incident = await run_in_threadpool(store.get, incident_id)
        if incident is None:
            raise HTTPException(404, f"incident {incident_id} not found")
        actions = incident.diagnosis.recommended_actions if incident.diagnosis else []
        match = next(((i, a) for i, a in enumerate(actions) if a.id == action_id), None)
        if match is None:
            raise HTTPException(404, f"recommendation {action_id} not found")
        index, action = match
        stale_before = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="milliseconds")
        if action.status == "EXECUTING" and (
            not action.changed_at or action.changed_at >= stale_before
        ):
            return incident
        if action.status not in {"APPROVED", "EXECUTING"}:
            return incident
        try:
            incident = await run_in_threadpool(
                store.change_recommendation,
                incident_id,
                index,
                action_id,
                action.status,
                "EXECUTING",
                action.decided_by or "engineer",
                f"Approved remediation started: {action_id}",
                stale_before=stale_before if action.status == "EXECUTING" else None,
            )
        except Conflict:
            return await run_in_threadpool(store.get, incident_id)
        await broadcast_incident(incident)
        try:
            if action.action != "clear_chaos" or action.service not in services_by_name:
                raise ValueError("recommendation is outside the remediation allowlist")
            await simulation.recover(client, services_by_name[action.service])
            if local:
                await monitor.poll_once()
                if monitor.snapshot[action.service].status != "healthy":
                    raise ValueError("service recovery was not verified by /health")
            outcome = "SUCCEEDED"
        except Exception:
            LOG.exception("Approved remediation failed for %s", incident_id)
            outcome = "FAILED"
        incident = await run_in_threadpool(
            store.change_recommendation,
            incident_id,
            index,
            action_id,
            "EXECUTING",
            outcome,
            action.decided_by or "engineer",
            f"Remediation {action_id} {outcome.lower()}",
        )
        await broadcast_incident(incident)
        if outcome == "SUCCEEDED":
            with contextlib.suppress(Exception):
                await monitor.poll_once()
        return incident

    async def check_background_work() -> None:
        while True:
            try:
                for incident in await run_in_threadpool(store.list_incidents, True):
                    if incident.diagnosis is None or incident.diagnosis.status == "RUNNING":
                        schedule_diagnosis(incident.incident_id)
                    elif incident.diagnosis.status == "READY":
                        for action in incident.diagnosis.recommended_actions:
                            if action.status in {"APPROVED", "EXECUTING"}:
                                await execute_recommendation(incident.incident_id, action.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Diagnosis recovery scan failed")
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal stopping
        task = asyncio.create_task(monitor.run(health_interval_s))
        queue_task = asyncio.create_task(consume_updates()) if queue_url else None
        diagnosis_task = (
            asyncio.create_task(check_background_work()) if queue_url or local else None
        )
        yield
        stopping = True
        task.cancel()
        if queue_task:
            queue_task.cancel()
        if diagnosis_task:
            diagnosis_task.cancel()
        for worker in diagnosis_tasks.values():
            worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if queue_task:
            with contextlib.suppress(asyncio.CancelledError):
                await queue_task
        if diagnosis_task:
            with contextlib.suppress(asyncio.CancelledError):
                await diagnosis_task
        for worker in list(diagnosis_tasks.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        if http_client is None:
            await client.aclose()
        if owns_local_store:
            store.close()

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

    @app.get("/api/environment")
    def environment():
        available = (
            bool(detection_checked_at)
            and not detection_error
            and (datetime.now(UTC) - datetime.fromisoformat(detection_checked_at)).total_seconds()
            < max(15, health_interval_s * 4)
        )
        return {
            "mode": mode,
            "incident_source": "local SQLite" if local else "AWS DynamoDB",
            "detection": {
                "source": "local health probes" if local else "AWS alarm pipeline",
                "status": ("available" if available else "unavailable")
                if local
                else ("configured" if queue_url else "queue_not_configured"),
                "checked_at": detection_checked_at,
                "failure_duration_s": local_failure_duration_s if local else None,
            },
            "diagnosis": {
                "provider": config.DIAGNOSIS_PROVIDER,
                "configuration": "missing_key"
                if config.DIAGNOSIS_PROVIDER == "gemini" and not os.getenv("GEMINI_API_KEY")
                else "configured",
            },
            "restart_commands": {
                s.name: f"docker compose start {s.name}"
                for s in services
                if local and s.name in {"gateway", "order", "payment", "inventory"}
            },
        }

    @app.get("/api/services")
    def list_services() -> list[ServiceHealth]:
        return monitor.current()

    @app.get("/api/services/graph")
    def service_graph() -> list[dict]:
        return GRAPH.as_api()

    @app.get("/api/services/{service_id}/dependencies")
    def service_dependencies(service_id: str) -> dict:
        if service_id not in GRAPH.nodes:
            raise HTTPException(404, f"unknown service {service_id}")
        node = GRAPH.nodes[service_id]
        return {
            "service": service_id,
            "direct": list(node.depends_on),
            "transitive": sorted(GRAPH.dependencies(service_id)),
            "dependents": sorted(GRAPH.dependents(service_id)),
        }

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

    @app.get("/api/incidents/{incident_id}/metrics")
    async def incident_metrics(incident_id: str) -> dict:
        incident = await run_in_threadpool(store.get, incident_id)
        if incident is None:
            raise HTTPException(404, f"incident {incident_id} not found")
        try:
            reader = telemetry or await run_in_threadpool(IncidentTelemetry.from_config)
            return await run_in_threadpool(reader.metrics, incident)
        except Exception as exc:
            LOG.exception("Could not load metrics for %s", incident_id)
            raise HTTPException(503, "Incident metrics are temporarily unavailable") from exc

    @app.get("/api/incidents/{incident_id}/logs")
    async def incident_logs(incident_id: str) -> dict:
        incident = await run_in_threadpool(store.get, incident_id)
        if incident is None:
            raise HTTPException(404, f"incident {incident_id} not found")
        timeline = await run_in_threadpool(store.timeline, incident_id)
        if not any(event.kind == "evidence" for event in timeline):
            return {"source": "saved CloudWatch error/warning excerpts", "rows": []}
        try:
            reader = telemetry or await run_in_threadpool(IncidentTelemetry.from_config)
            return await run_in_threadpool(reader.logs, incident, timeline)
        except Exception as exc:
            LOG.exception("Could not load logs for %s", incident_id)
            raise HTTPException(503, "Saved incident logs are temporarily unavailable") from exc

    @app.get("/api/incidents/{incident_id}/log-search")
    async def search_incident_logs(
        incident_id: str, q: str = Query(min_length=1, max_length=100)
    ) -> dict:
        incident = await run_in_threadpool(store.get, incident_id)
        if incident is None:
            raise HTTPException(404, f"incident {incident_id} not found")
        if not q.strip() or any(ord(char) < 32 for char in q):
            raise HTTPException(400, "search must be 1-100 printable characters")
        try:
            reader = telemetry or await run_in_threadpool(IncidentTelemetry.from_config)
            return await run_in_threadpool(reader.search_logs, incident, q)
        except Exception as exc:
            LOG.exception("Could not search CloudWatch logs for %s", incident_id)
            raise HTTPException(503, "CloudWatch log search is temporarily unavailable") from exc

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

    @app.post("/api/incidents/{incident_id}/recommendations/{action_id}/approve")
    async def approve_recommendation(
        incident_id: str, action_id: str, body: DiagnosisDecision
    ) -> Incident:
        incident = await run_in_threadpool(store.get, incident_id)
        if incident is None:
            raise HTTPException(404, f"incident {incident_id} not found")
        actions = incident.diagnosis.recommended_actions if incident.diagnosis else []
        match = next(((i, a) for i, a in enumerate(actions) if a.id == action_id), None)
        if match is None:
            raise HTTPException(404, f"recommendation {action_id} not found")
        index, action = match
        if action.status not in {"PENDING", "FAILED"}:
            raise HTTPException(409, "recommendation has already been decided")
        try:
            updated = await run_in_threadpool(
                store.change_recommendation,
                incident_id,
                index,
                action_id,
                action.status,
                "APPROVED",
                body.actor,
                f"Approved remediation: {action_id}",
            )
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        await broadcast_incident(updated)
        return await execute_recommendation(incident_id, action_id)

    @app.post("/api/incidents/{incident_id}/diagnosis/retry")
    async def retry_diagnosis(incident_id: str, body: DiagnosisDecision) -> Incident:
        if not local and not queue_url and diagnosis_engine is None:
            raise HTTPException(503, "AI analysis is not configured")
        try:
            updated = await run_in_threadpool(store.retry_diagnosis, incident_id, body.actor)
        except NotFound as exc:
            raise HTTPException(404, f"incident {incident_id} not found") from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        await broadcast_incident(updated)
        schedule_diagnosis(incident_id, delay_s=0)
        return updated

    @app.post("/api/incidents/{incident_id}/recommendations/{action_id}/reject")
    async def reject_recommendation(
        incident_id: str, action_id: str, body: DiagnosisDecision
    ) -> Incident:
        incident = await run_in_threadpool(store.get, incident_id)
        if incident is None:
            raise HTTPException(404, f"incident {incident_id} not found")
        actions = incident.diagnosis.recommended_actions if incident.diagnosis else []
        match = next(((i, a) for i, a in enumerate(actions) if a.id == action_id), None)
        if match is None:
            raise HTTPException(404, f"recommendation {action_id} not found")
        index, _ = match
        try:
            updated = await run_in_threadpool(
                store.change_recommendation,
                incident_id,
                index,
                action_id,
                "PENDING",
                "REJECTED",
                body.actor,
                f"Rejected remediation: {action_id}",
            )
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        await broadcast_incident(updated)
        return updated

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
        if local and monitor.snapshot[service.name].status != "healthy":
            raise HTTPException(
                409,
                f"{service.name} reset was accepted, but /health has not recovered. "
                "Recovery is not verified.",
            )
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
