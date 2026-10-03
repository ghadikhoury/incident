"""Pure, deterministic alert correlation, root analysis, and severity rules."""

import json
import math
from datetime import UTC, datetime

from incident_api.dependency import DependencyGraph
from incident_api.models import Alert, Severity


def alert_from_event(name: str, service: str, state: str, when: datetime, event: dict) -> Alert:
    reason = event["detail"]["state"].get("reasonData")
    try:
        data = json.loads(reason) if isinstance(reason, str) else reason or {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    evaluated = data.get("evaluatedDatapoints")
    if not isinstance(evaluated, list):
        evaluated = []
    values = [
        value
        for item in evaluated
        if isinstance(item, dict)
        if (value := _finite(item.get("value"))) is not None
    ]
    if not values:
        recent = data.get("recentDatapoints")
        if not isinstance(recent, list):
            recent = []
        values = [value for item in recent if (value := _finite(item)) is not None]
    onset = when
    if state == "ALARM":
        for point in evaluated:
            if not isinstance(point, dict) or _finite(point.get("value")) is None:
                continue
            try:
                measured = datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            if measured.tzinfo is not None:
                onset = min(onset, measured.astimezone(UTC))
    at = when.isoformat(timespec="milliseconds")
    return Alert(
        alarm_name=name,
        service=service,
        signal=name.rsplit("-", 1)[-1],
        state=state,
        first_at=onset.isoformat(timespec="milliseconds"),
        last_at=at,
        observed_value=max(values) if values else None,
        threshold=_finite(data.get("threshold")),
    )


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def upsert_alert(alerts: list[Alert], incoming: Alert) -> list[Alert]:
    """Keep one current state per CloudWatch alarm, preserving its first onset."""
    current = {alert.alarm_name: alert for alert in alerts}
    previous = current.get(incoming.alarm_name)
    if previous:
        incoming = incoming.model_copy(
            update={"first_at": min(previous.first_at, incoming.first_at)}
        )
    current[incoming.alarm_name] = incoming
    return sorted(current.values(), key=lambda alert: (alert.first_at, alert.alarm_name))


def compatible(graph: DependencyGraph, service: str, alerts: list[Alert]) -> bool:
    return all(graph.related(service, alert.service) for alert in alerts)


def analyze(alerts: list[Alert], graph: DependencyGraph) -> dict:
    """Compute fields stored with an incident from its observed alert states."""
    services = {alert.service for alert in alerts}
    first = {
        service: min(alert.first_at for alert in alerts if alert.service == service)
        for service in services
    }
    root = (
        min(
            services,
            key=lambda service: (
                -sum(
                    service in graph.dependencies(other) for other in services if other != service
                ),
                first[service],
                service,
            ),
        )
        if services
        else None
    )
    downstream = (
        sorted(
            service
            for service in services
            if service != root and root in graph.dependencies(service)
        )
        if root
        else []
    )
    severity, reason = severity_for(alerts, graph)
    return {
        "alerts": [alert.model_dump(mode="json") for alert in alerts],
        "probable_root": root,
        "downstream_services": downstream,
        "correlation_label": "PROBABLE CASCADING FAILURE" if downstream else None,
        "severity": severity.value,
        "severity_reason": reason,
        **({"service": root} if root else {}),
    }


def severity_for(alerts: list[Alert], graph: DependencyGraph) -> tuple[Severity, str]:
    active = [alert for alert in alerts if alert.state != "OK"]
    if not active:
        return Severity.SEV4, "All correlated alarms have recovered"
    ranked = sorted(
        ((severity_for_alert(alert, graph), alert) for alert in active),
        key=lambda pair: (pair[0], pair[1].service, pair[1].signal),
    )
    rank, strongest = ranked[-1]
    affected = {alert.service for alert in active if alert.state == "ALARM"}
    unavailable = {
        alert.service for alert in active if alert.signal == "health" and alert.state == "ALARM"
    }
    if len(unavailable) >= 2:
        return Severity.SEV1, f"Health checks are failing for {len(unavailable)} services"
    if len(affected) >= 3 and rank >= 2:
        return Severity.SEV1, f"{len(affected)} services have active alarms"
    severity = {1: Severity.SEV3, 2: Severity.SEV2, 3: Severity.SEV1}.get(rank, Severity.SEV4)
    value = strongest.observed_value
    if strongest.state == "INSUFFICIENT_DATA":
        reason = f"{strongest.service} monitoring data is missing"
    elif strongest.signal == "health":
        reason = (
            f"{strongest.service} health-check failure fraction {value:.0%}"
            if value is not None
            else f"{strongest.service} health alarm"
        )
    elif strongest.signal == "errors":
        reason = (
            f"{strongest.service} 5xx error rate {value:.1f}%"
            if value is not None
            else f"{strongest.service} 5xx error-rate alarm"
        )
    else:
        baseline = graph.nodes[strongest.service].baseline_latency_ms
        reason = (
            f"{strongest.service} p90 latency {value:.0f} ms vs {baseline:.0f} ms baseline"
            if value is not None and baseline is not None
            else f"{strongest.service} latency alarm"
        )
    return severity, reason


def severity_for_alert(alert: Alert, graph: DependencyGraph) -> int:
    """0=SEV-4, 1=SEV-3, 2=SEV-2, 3=SEV-1."""
    if alert.state == "INSUFFICIENT_DATA":
        return 0
    value = alert.observed_value
    if alert.signal == "health":
        return 3 if alert.service == "gateway" and (value is None or value >= 0.9) else 2
    if alert.signal == "errors":
        if value is not None and value >= 75 and alert.service == "gateway":
            return 3
        return 2 if value is not None and value >= 50 else 1
    baseline = graph.nodes[alert.service].baseline_latency_ms
    threshold = alert.threshold or 2000
    if value is not None and baseline and value >= max(threshold * 2, baseline * 10):
        return 2
    return 1
