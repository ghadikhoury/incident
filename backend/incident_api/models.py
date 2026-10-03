from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Severity(StrEnum):
    SEV1 = "SEV-1"  # major outage, critical functionality unavailable
    SEV2 = "SEV-2"  # critical service significantly degraded
    SEV3 = "SEV-3"  # localized or moderate degradation
    SEV4 = "SEV-4"  # minor operational issue


class Status(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    INVESTIGATING = "INVESTIGATING"
    MITIGATING = "MITIGATING"
    RESOLVED = "RESOLVED"


class Alert(BaseModel):
    alarm_name: str
    service: str
    signal: Literal["health", "errors", "latency"]
    state: Literal["ALARM", "OK", "INSUFFICIENT_DATA"]
    first_at: str
    last_at: str
    observed_value: float | None = None
    threshold: float | None = None


class RecommendedAction(BaseModel):
    id: str
    action: Literal["clear_chaos"]
    service: str
    reason: str
    status: Literal["PENDING", "APPROVED", "EXECUTING", "REJECTED", "SUCCEEDED", "FAILED"] = (
        "PENDING"
    )
    decided_by: str | None = None
    changed_at: str | None = None


class Diagnosis(BaseModel):
    status: Literal["RUNNING", "READY", "UNAVAILABLE"]
    claimed_at: str
    summary: str | None = None
    likely_root_cause: str | None = None
    confidence: Literal["low", "medium", "high"] | None = None
    evidence: list[str] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)


class Incident(BaseModel):
    incident_id: str
    title: str
    service: str
    severity: Severity
    status: Status
    trigger: str
    summary: str | None = None
    assigned_to: str | None = None
    created_at: str
    updated_at: str
    resolved_at: str | None = None
    alerts: list[Alert] = Field(default_factory=list)
    probable_root: str | None = None
    downstream_services: list[str] = Field(default_factory=list)
    correlation_label: str | None = None
    severity_reason: str | None = None
    diagnosis: Diagnosis | None = None


class TimelineEvent(BaseModel):
    at: str
    kind: str
    message: str
    actor: str | None = None


class IncidentDetail(Incident):
    timeline: list[TimelineEvent]


Actor = Field(default=None, max_length=64)


class IncidentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    service: str = Field(min_length=1, max_length=64)
    severity: Severity = Severity.SEV3
    trigger: str = Field(default="MANUAL", max_length=64)
    summary: str | None = Field(default=None, max_length=2000)
    actor: str | None = Actor


class IncidentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    severity: Severity | None = None
    status: Status | None = None
    assigned_to: str | None = Field(default=None, max_length=64)
    actor: str | None = Actor


class IncidentAction(BaseModel):
    actor: str | None = Actor
    note: str | None = Field(default=None, max_length=1000)


class DiagnosisDecision(BaseModel):
    actor: str = Field(min_length=1, max_length=64)

    @field_validator("actor")
    @classmethod
    def nonblank_actor(cls, value: str) -> str:
        actor = value.strip()
        if not actor:
            raise ValueError("actor must not be blank")
        return actor
