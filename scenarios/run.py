"""Run labeled failures against a live Incident demo and save measured outcomes.

Run from the deployed repository on EC2, where localhost:8000 reaches the backend:
    python3 scenarios/run.py --cases db_slow,intermittent --output results/step9.json
For crash, pass --allow-crash-restart so the runner may call Docker Compose.
The runner never approves an AI recommendation; it restores faults itself.
"""

import argparse
import csv
import json
import re
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CASES = {
    "db_slow": (
        "payment",
        "db_slow",
        r"connection.{0,25}pool|pool.{0,25}exhaust|pool.{0,25}timeout",
    ),
    "crash": ("payment", "crash", r"crash|unavailable|connection refused|service.{0,15}down"),
    "intermittent": ("inventory", "intermittent", r"intermittent|sporadic|transient|periodic"),
    "cpu": ("inventory", "cpu", r"cpu|processor|compute.{0,20}(load|saturat|pressure)"),
}
ACTOR = "scenario-runner"


def call(base_url: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"{method} {path}: HTTP {exc.code} {exc.read()[:300]!r}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {path}: {exc.reason}") from exc


def active_incidents(base_url: str) -> list[dict]:
    return call(base_url, "GET", "/api/incidents?active=true")


def healthy(base_url: str) -> bool:
    services = call(base_url, "GET", "/api/services")
    return bool(services) and all(
        service["status"] == "healthy" and not any((service.get("chaos") or {}).values())
        for service in services
    )


def wait_for_incident(base_url: str, baseline: set[str], timeout: float, poll: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fresh = [
            incident
            for incident in active_incidents(base_url)
            if incident["incident_id"] not in baseline and incident["trigger"] == "CLOUDWATCH"
        ]
        if fresh:
            return min(fresh, key=lambda row: row["created_at"])
        time.sleep(poll)
    return None


def wait_for_recovery(
    base_url: str, incident_id: str, timeout: float, poll: float, settle: float
) -> bool:
    deadline = time.monotonic() + timeout
    settled = time.monotonic() + settle
    consecutive = 0
    while time.monotonic() < deadline:
        incident = call(base_url, "GET", f"/api/incidents/{incident_id}")
        alerts = incident["alerts"]
        clear = bool(alerts) and all(alert["state"] == "OK" for alert in alerts)
        consecutive = consecutive + 1 if clear and healthy(base_url) else 0
        if consecutive >= 2 and time.monotonic() >= settled:
            return True
        time.sleep(poll)
    return False


def restore(base_url: str, service: str, failure: str, allow_crash_restart: bool) -> None:
    if failure == "crash":
        if not allow_crash_restart:
            raise RuntimeError("crash requires --allow-crash-restart")
        subprocess.run(["docker", "compose", "start", service], check=True, timeout=90)
    else:
        call(base_url, "POST", "/api/simulation/recover", {"service": service})


def write_results(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    with path.with_suffix(".csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run_case(base_url: str, name: str, args) -> dict:
    service, failure, cause_pattern = CASES[name]
    if active_incidents(base_url):
        raise RuntimeError("active incidents exist; resolve them before running an isolated case")
    if not healthy(base_url):
        raise RuntimeError("a service is unhealthy before injection")
    baseline = {row["incident_id"] for row in call(base_url, "GET", "/api/incidents")}
    started_at = datetime.now(UTC)
    started = time.monotonic()
    row = {
        "case": name,
        "service": service,
        "started_at": started_at.isoformat(),
        "incident_id": None,
        "time_to_detect_s": None,
        "alarms_per_incident": None,
        "probable_root": None,
        "root_service_correct": None,
        "ai_status": None,
        "ai_likely_root_cause": None,
        "ai_root_cause_correct": None,
        "recovered": False,
        "error": None,
    }
    injected = False
    try:
        # The crash response can be lost while the process exits. Always attempt
        # restoration, even if the injection request raises after taking effect.
        injected = True
        call(base_url, "POST", "/api/simulation/failure", {"service": service, "failure": failure})
        incident = wait_for_incident(base_url, baseline, args.max_detect_s, args.poll_s)
        if incident is None:
            raise RuntimeError("no CloudWatch incident before detection timeout")
        row["incident_id"] = incident["incident_id"]
        row["time_to_detect_s"] = round(time.monotonic() - started, 1)
        # Wait for the alarm cascade and the delayed Step 8 diagnosis.
        deadline = time.monotonic() + args.analysis_wait_s
        while time.monotonic() < deadline:
            detail = call(base_url, "GET", f"/api/incidents/{incident['incident_id']}")
            if (detail.get("diagnosis") or {}).get("status") in {"READY", "UNAVAILABLE"}:
                break
            time.sleep(args.poll_s)
        detail = call(base_url, "GET", f"/api/incidents/{incident['incident_id']}")
        row["alarms_per_incident"] = len(detail["alerts"])
        row["probable_root"] = detail.get("probable_root")
        row["root_service_correct"] = detail.get("probable_root") == service
        diagnosis = detail.get("diagnosis") or {}
        row["ai_status"] = diagnosis.get("status") or "NOT_RUN"
        if diagnosis.get("status") == "READY":
            cause = diagnosis.get("likely_root_cause") or ""
            row["ai_likely_root_cause"] = cause
            row["ai_root_cause_correct"] = bool(
                re.search(cause_pattern, cause, re.I)
                and re.search(rf"\b{re.escape(service)}\b", cause, re.I)
            )
    except Exception as exc:
        row["error"] = str(exc)
    finally:
        if injected:
            try:
                restore(base_url, service, failure, args.allow_crash_restart)
            except Exception as exc:
                row["error"] = "; ".join(filter(None, [row["error"], f"recovery failed: {exc}"]))
        if row["incident_id"] and not row["error"]:
            try:
                row["recovered"] = wait_for_recovery(
                    base_url,
                    row["incident_id"],
                    args.max_recovery_s,
                    args.poll_s,
                    args.recovery_settle_s,
                )
                if row["recovered"]:
                    call(
                        base_url,
                        "POST",
                        f"/api/incidents/{row['incident_id']}/resolve",
                        {"actor": ACTOR, "note": f"Evaluation case {name} recovered; alerts OK"},
                    )
                    unexpected = [
                        item["incident_id"]
                        for item in active_incidents(base_url)
                        if item["incident_id"] not in baseline
                    ]
                    if unexpected:
                        row["error"] = f"extra incident(s) remained active: {unexpected}"
                else:
                    row["error"] = "alarms or services did not recover before timeout"
            except Exception as exc:
                row["error"] = f"recovery verification failed: {exc}"
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cases", default="db_slow,intermittent,cpu")
    parser.add_argument("--output", type=Path, default=Path("scenarios/results/step9.json"))
    parser.add_argument("--max-detect-s", type=float, default=360)
    parser.add_argument("--analysis-wait-s", type=float, default=150)
    parser.add_argument("--max-recovery-s", type=float, default=480)
    parser.add_argument("--recovery-settle-s", type=float, default=90)
    parser.add_argument("--poll-s", type=float, default=5)
    parser.add_argument("--allow-crash-restart", action="store_true")
    args = parser.parse_args()
    cases = args.cases.split(",")
    if any(case not in CASES for case in cases) or not cases:
        parser.error(f"cases must be chosen from {', '.join(CASES)}")
    if "crash" in cases and not args.allow_crash_restart:
        parser.error("crash requires --allow-crash-restart")
    if min(args.max_detect_s, args.analysis_wait_s, args.max_recovery_s, args.poll_s) <= 0:
        parser.error("timeouts and poll interval must be positive")
    if args.recovery_settle_s < 0 or args.recovery_settle_s >= args.max_recovery_s:
        parser.error("recovery settle time must be nonnegative and shorter than recovery timeout")
    rows = []
    for case in cases:
        print(f"Running {case}...", flush=True)
        row = run_case(args.base_url, case, args)
        rows.append(row)
        write_results(args.output, rows)
        print(json.dumps(row), flush=True)
        if row["error"]:
            print(
                "Stopping after an incomplete case; inspect the demo before retrying.", flush=True
            )
            break
    detected = [row["time_to_detect_s"] for row in rows if row["time_to_detect_s"] is not None]
    roots = [row["root_service_correct"] for row in rows if row["root_service_correct"] is not None]
    ready = [
        row["ai_root_cause_correct"] for row in rows if row["ai_root_cause_correct"] is not None
    ]
    print(
        f"Detected {len(detected)}/{len(rows)}; median detection "
        f"{statistics.median(detected) if detected else 'N/A'} s; "
        f"root correct {sum(roots)}/{len(roots)}; "
        f"AI cause pattern match {sum(ready)}/{len(ready)} ready diagnoses "
        f"({len(rows) - len(ready)} unavailable/not run).",
        flush=True,
    )
    print("AI cause matching uses case-specific keyword patterns; review diagnoses manually.")
    return 1 if any(row["error"] for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
