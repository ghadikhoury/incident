from enum import StrEnum

from pydantic import BaseModel, Field


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
