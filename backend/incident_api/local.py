"""Durable local health detection and evidence. No AWS or Docker control clients."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from incident_api.correlation import analyze, compatible, upsert_alert
from incident_api.dependency import GRAPH
from incident_api.diagnosis.engine import _redact
from incident_api.models import Alert, Diagnosis, Incident, IncidentCreate, Status, TimelineEvent
from incident_api.store import LIST_LIMIT, Conflict, NotFound, now_iso

MAX_SAMPLES = 800


class LocalIncidentStore:
    """SQLite transactions keep incident changes, evidence, and audit entries together.

    One backend process owns the detector. Its debounce state survives restarts. The
    database contains only this demo's observations; no AWS identifiers are consulted.
    """

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY AUTOINCREMENT,
                document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (incident_id TEXT, document TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_incident ON events(incident_id);
            CREATE TABLE IF NOT EXISTS probes (service TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT, document TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_incident ON samples(incident_id);
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def _save(self, incident):
        self.db.execute(
            "UPDATE incidents SET document=? WHERE id=?",
            (incident.model_dump_json(), int(incident.incident_id.removeprefix("INC-")) - 1000),
        )

    def _events(self, incident_id, events, actor=None):
        for kind, message in events:
            event = TimelineEvent(at=now_iso(), kind=kind, message=message, actor=actor)
            self.db.execute(
                "INSERT INTO events VALUES (?, ?)", (incident_id, event.model_dump_json())
            )

    def _create(self, data, fields=None):
        cursor = self.db.execute("INSERT INTO incidents(document) VALUES ('{}')")
        at = now_iso()
        incident = Incident(
            incident_id=f"INC-{1000 + cursor.lastrowid}",
            status=Status.OPEN,
            created_at=at,
            updated_at=at,
            **data.model_dump(exclude={"actor"}),
        )
        if fields:
            incident = Incident.model_validate(incident.model_dump() | fields)
        self._save(incident)
        self._events(
            incident.incident_id, [("created", f"Incident created: {incident.title}")], data.actor
        )
        return incident

    def create(self, data: IncidentCreate) -> Incident:
        with self.transaction():
            return self._create(data)

    def get(self, incident_id: str) -> Incident | None:
        with self.lock:
            row = self.db.execute(
                "SELECT document FROM incidents WHERE id=?",
                (
                    int(incident_id[4:]) - 1000
                    if incident_id.startswith("INC-") and incident_id[4:].isdigit()
                    else -1,
                ),
            ).fetchone()
            return Incident.model_validate_json(row[0]) if row else None

    def list_incidents(self, active_only=False):
        with self.lock:
            incidents = [
                Incident.model_validate_json(row[0])
                for row in self.db.execute("SELECT document FROM incidents ORDER BY id DESC")
            ]
            return (
                [i for i in incidents if i.status != Status.RESOLVED]
                if active_only
                else incidents[:LIST_LIMIT]
            )

    def timeline(self, incident_id):
        with self.lock:
            return [
                TimelineEvent.model_validate_json(row[0])
                for row in self.db.execute(
                    "SELECT document FROM events WHERE incident_id=? ORDER BY rowid", (incident_id,)
                )
            ]

    def _require(self, incident_id, require_status=None):
        incident = self.get(incident_id)
        if incident is None:
            raise NotFound(incident_id)
        if incident.status == Status.RESOLVED or (
            require_status and incident.status != require_status
        ):
            raise Conflict(f"{incident_id} is {incident.status}")
        return incident

    def update(self, incident_id, changes, events, actor, *, require_status=None):
        with self.transaction():
            incident = self._require(incident_id, require_status)
            fields = changes | {"updated_at": now_iso()}
            if "title" in changes:
                fields["title_source"] = "manual"
            if changes.get("status") == Status.RESOLVED:
                fields["resolved_at"] = fields["updated_at"]
            incident = Incident.model_validate(incident.model_dump() | fields)
            self._save(incident)
            self._events(incident_id, events, actor)
            return incident

    def claim_diagnosis(self, incident_id):
        with self.transaction():
            incident = self.get(incident_id)
            if incident is None or incident.status == Status.RESOLVED:
                return None
            cutoff = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
            if incident.diagnosis and not (
                incident.diagnosis.status == "RUNNING" and incident.diagnosis.claimed_at < cutoff
            ):
                return None
            claim = now_iso()
            incident.diagnosis = Diagnosis(status="RUNNING", claimed_at=claim)
            incident.updated_at = claim
            self._save(incident)
            self._events(incident_id, [("diagnosis", "AI analysis started")], "ai")
            return claim

    def finish_diagnosis(self, incident_id, claim, diagnosis):
        with self.transaction():
            incident = self.get(incident_id)
            if (
                not incident
                or not incident.diagnosis
                or incident.diagnosis.status != "RUNNING"
                or incident.diagnosis.claimed_at != claim
            ):
                return None
            incident.diagnosis = diagnosis
            incident.updated_at = now_iso()
            self._save(incident)
            message = (
                "AI analysis ready for review"
                if diagnosis.status == "READY"
                else "AI analysis unavailable"
            )
            self._events(incident_id, [("diagnosis", message)], "ai")
            return incident

    def retry_diagnosis(self, incident_id, actor):
        with self.transaction():
            incident = self._require(incident_id)
            if not incident.diagnosis or incident.diagnosis.status != "UNAVAILABLE":
                raise Conflict(f"{incident_id} has no unavailable diagnosis to retry")
            incident.diagnosis = None
            incident.updated_at = now_iso()
            self._save(incident)
            self._events(incident_id, [("diagnosis", "AI analysis retry requested")], actor)
            return incident

    def change_recommendation(
        self,
        incident_id,
        index,
        action_id,
        from_status,
        to_status,
        actor,
        message,
        *,
        stale_before=None,
    ):
        with self.transaction():
            incident = self.get(incident_id)
            if incident is None:
                raise NotFound(incident_id)
            diagnosis = incident.diagnosis
            actions = diagnosis.recommended_actions if diagnosis else []
            action = actions[index] if 0 <= index < len(actions) else None
            if (
                not diagnosis
                or diagnosis.status != "READY"
                or not action
                or action.id != action_id
                or action.status != from_status
                or (incident.status == Status.RESOLVED and to_status not in {"SUCCEEDED", "FAILED"})
                or (stale_before and (not action.changed_at or action.changed_at >= stale_before))
            ):
                raise Conflict(f"recommendation {action_id} is no longer {from_status}")
            action.status, action.decided_by, action.changed_at = to_status, actor, now_iso()
            incident.updated_at = action.changed_at
            self._save(incident)
            self._events(incident_id, [("remediation", message)], actor)
            return incident

    def observe(self, snapshot, persistence_s=9, min_failures=3, max_gap_s=15):
        """Process actual health probes; injection settings never enter this method.

        Failure requires consecutive probes AND elapsed duration. Recovery requires
        two healthy probes. A resolved, still-failing service must recover to rearm.
        """
        changed = {}
        with self.transaction():
            active = self.list_incidents(True)
            samples = []
            for health in snapshot:
                if health.status == "unknown" or not health.checked_at:
                    continue
                sample = _redact(
                    {
                        "service": health.name,
                        "@timestamp": health.checked_at,
                        "status": health.status,
                        "response_ms": health.response_ms,
                        "message": f"Observed /health: {health.status}",
                        "error_message": (health.error or "")[:300],
                    }
                )
                samples.append(sample)
                row = self.db.execute(
                    "SELECT document FROM probes WHERE service=?", (health.name,)
                ).fetchone()
                state = json.loads(row[0]) if row else {"alarming": False, "count": 0, "healthy": 0}
                at = datetime.fromisoformat(health.checked_at)
                if state.get("last") and at <= datetime.fromisoformat(state["last"]):
                    continue  # one observation cannot count as multiple probes
                if (
                    state.get("last")
                    and (at - datetime.fromisoformat(state["last"])).total_seconds() > max_gap_s
                ):
                    state.update(count=0, healthy=0)
                state["last"] = health.checked_at
                state["history"] = (state.get("history", []) + [sample])[-12:]
                failed = health.status in {"down", "unhealthy"}
                if failed:
                    if not state["count"]:
                        state["first"] = health.checked_at
                    state["count"] += 1
                    state["healthy"] = 0
                else:
                    if not state["healthy"]:
                        state["healthy_first"] = health.checked_at
                    state["count"] = 0
                    state["healthy"] += 1
                duration = (
                    at - datetime.fromisoformat(state.get("first", health.checked_at))
                ).total_seconds()
                transition = None
                if (
                    failed
                    and not state["alarming"]
                    and state["count"] >= min_failures
                    and duration >= persistence_s
                ):
                    state["alarming"] = True
                    transition = "ALARM"
                elif (
                    not failed
                    and state["alarming"]
                    and state["healthy"] >= 2
                    and (at - datetime.fromisoformat(state["healthy_first"])).total_seconds() >= 3
                ):
                    state["alarming"] = False
                    transition = "OK"
                self.db.execute(
                    "INSERT OR REPLACE INTO probes VALUES (?, ?)", (health.name, json.dumps(state))
                )
                if not transition:
                    continue
                alarm = f"local-{health.name}-health"
                incident = next(
                    (i for i in active if any(a.alarm_name == alarm for a in i.alerts)), None
                )
                if incident is None and transition == "ALARM":
                    incident = next(
                        (
                            i
                            for i in active
                            if i.trigger == "LOCAL_HEALTH"
                            and compatible(GRAPH, health.name, i.alerts)
                            and i.alerts
                            and (
                                any(a.state == "ALARM" for a in i.alerts)
                                or abs(
                                    (
                                        at
                                        - datetime.fromisoformat(max(a.last_at for a in i.alerts))
                                    ).total_seconds()
                                )
                                <= 600
                            )
                        ),
                        None,
                    )
                if incident is None and transition == "OK":
                    continue
                alert = Alert(
                    alarm_name=alarm,
                    service=health.name,
                    signal="health",
                    state=transition,
                    first_at=state.get("first", health.checked_at),
                    last_at=health.checked_at,
                    observed_value=1 if failed else 0,
                    threshold=1,
                )
                fields = analyze(upsert_alert(incident.alerts if incident else [], alert), GRAPH)
                if incident is None:
                    incident = self._create(
                        IncidentCreate(
                            title=f"{health.name} incident",
                            service=health.name,
                            trigger="LOCAL_HEALTH",
                        ),
                        fields | {"title_source": "generated"},
                    )
                    active.append(incident)
                    self.db.executemany(
                        "INSERT INTO samples(incident_id, document) VALUES (?, ?)",
                        [
                            (incident.incident_id, json.dumps(probe))
                            for probe in state["history"][:-1]
                        ],
                    )
                    self._events(
                        incident.incident_id,
                        [
                            (
                                "evidence",
                                "Collected local /health observations (no injection settings)",
                            )
                        ],
                    )
                else:
                    if incident.title_source == "generated":
                        fields["title"] = f"{fields['probable_root']} incident"
                    incident = Incident.model_validate(
                        incident.model_dump() | fields | {"updated_at": now_iso()}
                    )
                    active = [
                        incident if i.incident_id == incident.incident_id else i for i in active
                    ]
                    self._save(incident)
                self._events(
                    incident.incident_id,
                    [("observation", f"{health.name} health {transition} at {health.checked_at}")],
                )
                changed[incident.incident_id] = incident
            for incident in active:
                if incident.trigger != "LOCAL_HEALTH":
                    continue
                self.db.executemany(
                    "INSERT INTO samples(incident_id, document) VALUES (?, ?)",
                    [(incident.incident_id, json.dumps(sample)) for sample in samples],
                )
                # Keep initial failure evidence plus a bounded rolling tail (including recovery).
                count = self.db.execute(
                    "SELECT COUNT(*) FROM samples WHERE incident_id=?", (incident.incident_id,)
                ).fetchone()[0]
                if count > MAX_SAMPLES:
                    self.db.execute(
                        "DELETE FROM samples WHERE id IN (SELECT id FROM samples "
                        "WHERE incident_id=? ORDER BY id LIMIT ? OFFSET 100)",
                        (incident.incident_id, count - MAX_SAMPLES),
                    )
        return list(changed.values())

    def evidence(self, incident_id):
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT document FROM samples WHERE incident_id=? ORDER BY id", (incident_id,)
                )
            ]


class LocalTelemetry:
    def __init__(self, store):
        self.store = store

    def logs(self, incident, timeline=None):
        rows = [
            {key: str(value) for key, value in row.items()}
            for row in self.store.evidence(incident.incident_id)
        ]
        return {"source": "collected local health probes (not container logs)", "rows": rows}

    def search_logs(self, incident, query):
        result = self.logs(incident)
        result["rows"] = [row for row in result["rows"] if query.lower() in json.dumps(row).lower()]
        return result

    def metrics(self, incident):
        rows = self.store.evidence(incident.incident_id)
        series = []
        for service in sorted({row["service"] for row in rows}):
            probes = [row for row in rows if row["service"] == service]
            for metric in ("HealthCheckFailed", "HealthLatency"):
                points = [
                    {
                        "at": row["@timestamp"],
                        "value": (
                            int(row["status"] != "healthy")
                            if metric == "HealthCheckFailed"
                            else row["response_ms"]
                        ),
                    }
                    for row in probes
                    if metric == "HealthCheckFailed" or row["response_ms"] is not None
                ]
                series.append({"service": service, "metric": metric, "points": points})
        return {
            "start": rows[0]["@timestamp"] if rows else incident.created_at,
            "end": rows[-1]["@timestamp"] if rows else incident.updated_at,
            "series": series,
        }
