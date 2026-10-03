"""CloudWatch metrics carried inside log lines (Embedded Metric Format, "EMF").

The health monitor reports each service's health check as a metric. This covers what a
service's own request metrics can't: a crashed or hung service emits nothing at all.
"""

import json
import os
import sys
import time

NAMESPACE = os.getenv("INCIDENT_METRIC_NAMESPACE", "Incident")


def health_check_line(
    service: str, failed: bool, status: str, error: str | None, timestamp_ms: int
) -> dict:
    return {
        "_aws": {
            "Timestamp": timestamp_ms,
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [["Service"]],
                    "Metrics": [{"Name": "HealthCheckFailed", "Unit": "Count"}],
                }
            ],
        },
        "message": "health check",
        "Service": service,
        "HealthCheckFailed": 1 if failed else 0,
        "status": status,
        "error": error,
    }


def emit_health_checks(results) -> None:
    """One EMF line per service on stdout (which the container's log driver ships)."""
    now_ms = int(time.time() * 1000)
    for result in results:
        line = health_check_line(
            result.name, result.status != "healthy", result.status, result.error, now_ms
        )
        sys.stdout.write(json.dumps(line) + "\n")
    sys.stdout.flush()
