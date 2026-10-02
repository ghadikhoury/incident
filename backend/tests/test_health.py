import asyncio

from incident_api.config import MonitoredService
from incident_api.health import HealthMonitor

SERVICES = (
    MonitoredService("payment", "Payments", "http://payment", ("postgres",)),
    MonitoredService("order", "Orders", "http://order", ("payment",)),
)


def make_monitor(fake):
    changes = []

    async def on_change(snapshot):
        changes.append({s.name: s.status for s in snapshot})

    return HealthMonitor(SERVICES, fake.client(), 1.0, on_change), changes


def test_starts_unknown(fake):
    monitor, _ = make_monitor(fake)
    assert {s.status for s in monitor.current()} == {"unknown"}


def test_reports_healthy_unhealthy_and_down(fake):
    fake.unhealthy["payment"] = "database connection pool exhausted"
    monitor, _ = make_monitor(fake)
    asyncio.run(monitor.poll_once())
    by_name = {s.name: s for s in monitor.current()}
    assert by_name["payment"].status == "unhealthy"
    assert by_name["payment"].error == "database connection pool exhausted"
    assert by_name["order"].status == "healthy"
    assert by_name["order"].depends_on == ["payment"]

    fake.down.add("order")
    asyncio.run(monitor.poll_once())
    order = {s.name: s for s in monitor.current()}["order"]
    assert order.status == "down"
    assert order.error.startswith("unreachable")


def test_captures_active_chaos(fake):
    fake.chaos["payment"]["db_delay_s"] = 3.0
    monitor, _ = make_monitor(fake)
    asyncio.run(monitor.poll_once())
    assert {s.name: s for s in monitor.current()}["payment"].chaos["db_delay_s"] == 3.0


def test_only_notifies_when_something_changes(fake):
    monitor, changes = make_monitor(fake)
    asyncio.run(monitor.poll_once())
    asyncio.run(monitor.poll_once())
    assert len(changes) == 1  # unknown -> healthy; the second poll changed nothing
    fake.down.add("payment")
    asyncio.run(monitor.poll_once())
    assert changes[-1] == {"payment": "down", "order": "healthy"}
