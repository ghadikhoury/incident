"""CloudWatch metrics carried inside log lines (Embedded Metric Format, "EMF").

When a JSON log line contains an "_aws" block like the one below, CloudWatch Logs turns the
named fields into CloudWatch metrics. No separate metrics pipeline or API calls needed:
https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html
"""

import time

NAMESPACE = "Incident"


def request_metrics(service: str, latency_ms: float, status_code: int, timestamp_ms: int) -> dict:
    """Fields to add to a request's log line as CloudWatch metric observations."""
    return {
        "_aws": {
            "Timestamp": timestamp_ms,
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [["Service"]],
                    "Metrics": [
                        {"Name": "Latency", "Unit": "Milliseconds"},
                        {"Name": "Requests", "Unit": "Count"},
                        {"Name": "Errors", "Unit": "Count"},
                    ],
                }
            ],
        },
        "Service": service,
        "Latency": latency_ms,
        "Requests": 1,
        "Errors": 1 if status_code >= 500 else 0,
    }


def cpu_utilization_line(service: str, percent: float) -> dict:
    """Actual process CPU time divided by elapsed wall time since the last sample."""
    return {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [["Service"]],
                    "Metrics": [{"Name": "CpuUtilizationPct", "Unit": "Percent"}],
                }
            ],
        },
        "Service": service,
        "CpuUtilizationPct": round(percent, 1),
    }
