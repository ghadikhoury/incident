"""The deterministic decisions that drive Step 7 incidents."""

from datetime import UTC, datetime

import pytest

from incident_api.correlation import alert_from_event, analyze, severity_for, upsert_alert
from incident_api.dependency import GRAPH, DependencyGraph, ServiceNode
from incident_api.models import Alert, Severity


def alert(service, signal, value=None, state="ALARM", first="2026-10-02T12:00:00Z"):
    return Alert(
        alarm_name=f"incident-{service}-{signal}",
        service=service,
        signal=signal,
        state=state,
        first_at=first,
        last_at=first,
        observed_value=value,
    )


def test_graph_distinguishes_ancestor_cascade_from_sibling_faults():
    assert GRAPH.related("gateway", "payment")
    assert GRAPH.related("order", "inventory")
    assert not GRAPH.related("payment", "inventory")
    assert GRAPH.dependencies("gateway") == {"order", "payment", "inventory", "postgres"}
    assert GRAPH.dependents("payment") == {"gateway", "order"}


def test_dependency_cycle_is_rejected():
    nodes = {
        "a": ServiceNode("a", "A", ("b",), True, 10),
        "b": ServiceNode("b", "B", ("a",), True, 10),
    }
    with pytest.raises(ValueError, match="cycle"):
        DependencyGraph(nodes)


def test_root_prefers_deepest_dependency_even_if_its_alarm_arrives_later():
    alerts = [
        alert("gateway", "errors", 100),
        alert("order", "errors", 65, first="2026-10-02T12:01:00Z"),
        alert("payment", "latency", 4900, first="2026-10-02T12:02:00Z"),
    ]
    analysis = analyze(alerts, GRAPH)
    assert analysis["probable_root"] == "payment"
    assert analysis["downstream_services"] == ["gateway", "order"]
    assert analysis["correlation_label"] == "PROBABLE CASCADING FAILURE"
    assert analysis["severity"] == "SEV-1"


@pytest.mark.parametrize(
    ("alerts", "expected"),
    [
        ([alert("payment", "latency", 2500)], Severity.SEV3),
        ([alert("payment", "latency", 4900)], Severity.SEV2),
        ([alert("payment", "errors", 60)], Severity.SEV2),
        ([alert("gateway", "errors", 90)], Severity.SEV1),
        ([alert("payment", "health", 1)], Severity.SEV2),
        ([alert("payment", "health", 1), alert("order", "health", 1)], Severity.SEV1),
        ([alert("payment", "health", state="INSUFFICIENT_DATA")], Severity.SEV4),
        ([alert("payment", "errors", state="OK")], Severity.SEV4),
    ],
)
def test_severity_rules(alerts, expected):
    assert severity_for(alerts, GRAPH)[0] == expected


def test_event_metric_is_parsed_and_recovery_preserves_first_onset():
    event = {
        "detail": {
            "state": {"reasonData": '{"evaluatedDatapoints":[{"value":4900}],"threshold":2000}'}
        }
    }
    first = alert_from_event(
        "incident-payment-latency", "payment", "ALARM", datetime(2026, 10, 2, tzinfo=UTC), event
    )
    assert first.observed_value == 4900
    assert first.threshold == 2000
    later = first.model_copy(update={"state": "OK", "first_at": "2026-10-02T12:00:00Z"})
    [combined] = upsert_alert([first], later)
    assert combined.first_at == first.first_at
    assert combined.state == "OK"


def test_first_onset_comes_from_breaching_metric_period_not_alarm_delivery():
    event = {
        "detail": {
            "state": {
                "reasonData": (
                    '{"evaluatedDatapoints":[{"value":4900,'
                    '"timestamp":"2026-10-02T12:00:00.000+0000"}]}'
                )
            }
        }
    }
    first = alert_from_event(
        "incident-payment-latency",
        "payment",
        "ALARM",
        datetime(2026, 10, 2, 12, 2, tzinfo=UTC),
        event,
    )
    assert first.first_at == "2026-10-02T12:00:00.000+00:00"
    assert first.last_at == "2026-10-02T12:02:00.000+00:00"
