import pytest

from incident_api.models import IncidentCreate, Severity, Status
from incident_api.store import Conflict, NotFound


def new(store, title="Payment latency spike", **kwargs):
    return store.create(IncidentCreate(title=title, service="payment", **kwargs))


def test_create_assigns_sequential_ids_and_starts_open(store):
    first, second = new(store), new(store)
    assert (first.incident_id, second.incident_id) == ("INC-1001", "INC-1002")
    assert first.status == Status.OPEN
    assert first.severity == Severity.SEV3
    assert store.get("INC-1001") == first


def test_create_records_timeline_event(store):
    incident = new(store, actor="alice")
    [event] = store.timeline(incident.incident_id)
    assert event.kind == "created"
    assert event.actor == "alice"
    assert "Payment latency spike" in event.message


def test_get_missing_returns_none(store):
    assert store.get("INC-9999") is None


def test_list_is_newest_first_and_can_hide_resolved(store):
    new(store, "a")
    b = new(store, "b")
    new(store, "c")
    store.update(b.incident_id, {"status": Status.RESOLVED})
    assert [i.title for i in store.list_incidents()] == ["c", "b", "a"]
    assert [i.title for i in store.list_incidents(active_only=True)] == ["c", "a"]


def test_update_sets_and_removes_fields(store):
    incident = new(store)
    updated = store.update(incident.incident_id, {"severity": Severity.SEV1, "assigned_to": "bob"})
    assert updated.severity == Severity.SEV1
    assert updated.assigned_to == "bob"
    assert updated.updated_at >= incident.updated_at
    unassigned = store.update(incident.incident_id, {"assigned_to": None})
    assert unassigned.assigned_to is None


def test_resolving_sets_resolved_at_and_is_final(store):
    incident = new(store)
    resolved = store.update(incident.incident_id, {"status": Status.RESOLVED})
    assert resolved.resolved_at is not None
    with pytest.raises(Conflict):
        store.update(incident.incident_id, {"severity": Severity.SEV1})


def test_require_status_prevents_double_acknowledge(store):
    incident = new(store)
    ack = {"status": Status.ACKNOWLEDGED}
    store.update(incident.incident_id, ack, require_status=Status.OPEN)
    with pytest.raises(Conflict):
        store.update(incident.incident_id, ack, require_status=Status.OPEN)


def test_update_missing_incident_raises_not_found(store):
    with pytest.raises(NotFound):
        store.update("INC-9999", {"severity": Severity.SEV1})


def test_timeline_is_in_time_order(store):
    incident = new(store)
    store.add_event(incident.incident_id, "note", "first", None)
    store.add_event(incident.incident_id, "note", "second", None)
    messages = [e.message for e in store.timeline(incident.incident_id)]
    assert messages[1:] == ["first", "second"]
