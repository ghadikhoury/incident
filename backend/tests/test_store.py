import pytest
from botocore.exceptions import ClientError

from incident_api import store as store_module
from incident_api.models import IncidentCreate, Severity, Status
from incident_api.store import Conflict, NotFound


def new(store, title="Payment latency spike", **kwargs):
    return store.create(IncidentCreate(title=title, service="payment", **kwargs))


def update(store, incident_id, changes, events=(), actor=None, **kwargs):
    return store.update(incident_id, changes, list(events), actor, **kwargs)


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


def test_failed_create_does_not_consume_incident_number(store, monkeypatch):
    real_transact = store.client.transact_write_items
    failed = False

    def flaky_transact(**kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "TransactWriteItems")
        return real_transact(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", flaky_transact)
    with pytest.raises(ClientError):
        new(store)
    assert new(store).incident_id == "INC-1001"


def test_get_missing_returns_none(store):
    assert store.get("INC-9999") is None


def test_list_is_newest_first_and_can_hide_resolved(store):
    new(store, "a")
    b = new(store, "b")
    new(store, "c")
    update(store, b.incident_id, {"status": Status.RESOLVED})
    assert [i.title for i in store.list_incidents()] == ["c", "b", "a"]
    assert [i.title for i in store.list_incidents(active_only=True)] == ["c", "a"]


def test_old_open_incident_stays_listed_behind_many_resolved_ones(store):
    """Regression: the active list used to be filtered from the newest LIST_LIMIT incidents."""
    old_open = new(store, "old but still open")
    for i in range(store_module.LIST_LIMIT + 5):
        update(store, new(store, f"resolved {i}").incident_id, {"status": Status.RESOLVED})
    active = store.list_incidents(active_only=True)
    assert [i.incident_id for i in active] == [old_open.incident_id]


def test_update_sets_and_removes_fields_and_records_events(store):
    incident = new(store)
    updated = update(
        store,
        incident.incident_id,
        {"severity": Severity.SEV1, "assigned_to": "bob"},
        [("severity", "Severity set to SEV-1"), ("assignment", "Assigned to bob")],
        actor="carol",
    )
    assert updated.severity == Severity.SEV1
    assert updated.assigned_to == "bob"
    assert updated.updated_at >= incident.updated_at
    events = store.timeline(incident.incident_id)
    assert [e.message for e in events[1:]] == ["Severity set to SEV-1", "Assigned to bob"]
    assert {e.actor for e in events[1:]} == {"carol"}

    unassigned = update(store, incident.incident_id, {"assigned_to": None})
    assert unassigned.assigned_to is None


def test_resolving_sets_resolved_at_and_is_final(store):
    incident = new(store)
    resolved = update(store, incident.incident_id, {"status": Status.RESOLVED})
    assert resolved.resolved_at is not None
    with pytest.raises(Conflict):
        update(store, incident.incident_id, {"severity": Severity.SEV1})


def test_require_status_prevents_double_acknowledge(store):
    incident = new(store)
    ack = {"status": Status.ACKNOWLEDGED}
    update(
        store, incident.incident_id, ack, [("status", "Acknowledged")], require_status=Status.OPEN
    )
    with pytest.raises(Conflict):
        update(
            store,
            incident.incident_id,
            ack,
            [("status", "Acknowledged")],
            require_status=Status.OPEN,
        )
    assert [e.message for e in store.timeline(incident.incident_id)].count("Acknowledged") == 1


def test_update_missing_incident_raises_not_found(store):
    with pytest.raises(NotFound):
        update(store, "INC-9999", {"severity": Severity.SEV1})


def test_failed_write_changes_nothing_and_can_be_retried(store, monkeypatch):
    """Regression: the state change and its timeline event used to be separate writes."""
    incident = new(store)
    real_transact = store.client.transact_write_items
    failures = [ClientError({"Error": {"Code": "InternalServerError"}}, "TransactWriteItems")]

    def flaky_transact(**kwargs):
        if failures:
            raise failures.pop()
        return real_transact(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", flaky_transact)
    resolve = ({"status": Status.RESOLVED}, [("status", "Resolved")])

    with pytest.raises(ClientError):
        update(store, incident.incident_id, *resolve)
    assert store.get(incident.incident_id).status == Status.OPEN
    assert [e.kind for e in store.timeline(incident.incident_id)] == ["created"]

    retried = update(store, incident.incident_id, *resolve)  # a retry now succeeds
    assert retried.status == Status.RESOLVED
    assert [e.message for e in store.timeline(incident.incident_id)][-1] == "Resolved"


def test_timeline_keeps_events_in_order(store):
    incident = new(store)
    events = [("note", f"event {n}") for n in range(10)]
    update(store, incident.incident_id, {"title": "renamed"}, events)
    messages = [e.message for e in store.timeline(incident.incident_id)]
    assert messages[1:] == [f"event {n}" for n in range(10)]
