from types import SimpleNamespace

from scenarios import run as runner


def args():
    return SimpleNamespace(
        max_detect_s=1,
        poll_s=0.1,
        analysis_wait_s=1,
        max_recovery_s=1,
        recovery_settle_s=0,
        allow_crash_restart=False,
    )


def test_unavailable_ai_is_not_scored_as_incorrect(monkeypatch):
    restored = []
    monkeypatch.setattr(runner, "active_incidents", lambda base: [])
    monkeypatch.setattr(runner, "healthy", lambda base: True)
    monkeypatch.setattr(
        runner,
        "wait_for_incident",
        lambda *items: {
            "incident_id": "INC-1001",
            "created_at": "2026-10-03T00:00:00Z",
        },
    )
    monkeypatch.setattr(runner, "wait_for_recovery", lambda *items: True)
    monkeypatch.setattr(runner, "restore", lambda *items: restored.append(items[2]))

    def fake_call(_base, method, path, body=None):
        if path == "/api/incidents":
            return []
        if path.endswith("/INC-1001"):
            return {
                "alerts": [{}, {}],
                "probable_root": "payment",
                "diagnosis": {"status": "UNAVAILABLE"},
            }
        return {}

    monkeypatch.setattr(runner, "call", fake_call)
    row = runner.run_case("http://demo", "db_slow", args())
    assert row["alarms_per_incident"] == 2
    assert row["root_service_correct"] is True
    assert row["ai_status"] == "UNAVAILABLE"
    assert row["ai_root_cause_correct"] is None
    assert row["recovered"] is True
    assert restored == ["db_slow"]


def test_detection_timeout_restores_fault_without_claiming_a_result(monkeypatch):
    restored = []
    monkeypatch.setattr(runner, "active_incidents", lambda base: [])
    monkeypatch.setattr(runner, "healthy", lambda base: True)
    monkeypatch.setattr(runner, "wait_for_incident", lambda *items: None)
    monkeypatch.setattr(runner, "restore", lambda *items: restored.append(items[2]))
    monkeypatch.setattr(runner, "call", lambda *items, **kwargs: [])
    row = runner.run_case("http://demo", "intermittent", args())
    assert row["time_to_detect_s"] is None
    assert row["root_service_correct"] is None
    assert "detection timeout" in row["error"]
    assert restored == ["intermittent"]
