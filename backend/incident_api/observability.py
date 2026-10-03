"""Bounded, read-only incident telemetry for the detail page.

CloudWatch is the source for plotted metrics. The saved S3 evidence supplies the
error/warning log excerpts, so the page remains useful after log retention expires.
"""

import json
import time
from datetime import UTC, datetime, timedelta

import boto3

from incident_api import config
from incident_api.diagnosis.engine import MAX_OBJECT_BYTES, DiagnosisEngine
from incident_api.models import Incident, TimelineEvent
from incident_api.pipeline import EXCLUDE_INJECTION

MAX_EVENTS = 12
MAX_LOG_ROWS = 800
METRICS = (("Latency", "p90"), ("Requests", "Sum"), ("Errors", "Sum"))


class IncidentTelemetry:
    def __init__(self, cloudwatch, s3, bucket: str, cloudwatch_logs=None):
        self.cloudwatch = cloudwatch
        self.s3 = s3
        self.bucket = bucket
        self.cloudwatch_logs = cloudwatch_logs

    @classmethod
    def from_config(cls) -> "IncidentTelemetry":
        session = boto3.Session(profile_name=config.AWS_PROFILE, region_name=config.AWS_REGION)
        bucket = config.EVIDENCE_BUCKET or (
            "incident-evidence-" + session.client("sts").get_caller_identity()["Account"]
        )
        return cls(
            session.client("cloudwatch"), session.client("s3"), bucket, session.client("logs")
        )

    @staticmethod
    def window(incident: Incident) -> tuple[datetime, datetime]:
        created = datetime.fromisoformat(incident.created_at).astimezone(UTC)
        start = created - timedelta(minutes=5)
        end = min(
            datetime.now(UTC),
            created + timedelta(minutes=25),
            datetime.fromisoformat(incident.resolved_at).astimezone(UTC) + timedelta(minutes=5)
            if incident.resolved_at
            else datetime.max.replace(tzinfo=UTC),
        )
        return start, max(start + timedelta(seconds=1), end)

    @staticmethod
    def services(incident: Incident) -> list[str]:
        monitored = {service.name for service in config.SERVICES}
        names = [incident.probable_root, incident.service]
        names += [alert.service for alert in incident.alerts]
        names += incident.downstream_services
        return list(dict.fromkeys(name for name in names if name in monitored))[:4]

    def metrics(self, incident: Incident) -> dict:
        start, end = self.window(incident)
        queries = []
        labels = {}
        for service in self.services(incident):
            for metric, statistic in METRICS:
                query_id = f"m{len(queries)}"
                labels[query_id] = (service, metric)
                queries.append(
                    {
                        "Id": query_id,
                        "MetricStat": {
                            "Metric": {
                                "Namespace": config.METRIC_NAMESPACE,
                                "MetricName": metric,
                                "Dimensions": [{"Name": "Service", "Value": service}],
                            },
                            "Period": 60,
                            "Stat": statistic,
                        },
                    }
                )
        series = []
        if queries:
            response = self.cloudwatch.get_metric_data(
                MetricDataQueries=queries,
                StartTime=start,
                EndTime=end,
                ScanBy="TimestampAscending",
                MaxDatapoints=5000,
            )
            if response.get("NextToken"):
                raise RuntimeError("metric data exceeded the bounded incident window")
            for row in response.get("MetricDataResults", []):
                if row.get("Id") not in labels:
                    continue
                service, metric = labels[row["Id"]]
                points = sorted(zip(row.get("Timestamps", []), row.get("Values", []), strict=True))
                series.append(
                    {
                        "service": service,
                        "metric": metric,
                        "points": [
                            {"at": stamp.astimezone(UTC).isoformat(), "value": value}
                            for stamp, value in points
                        ],
                    }
                )
        return {"start": start.isoformat(), "end": end.isoformat(), "series": series}

    def logs(self, incident: Incident, timeline: list[TimelineEvent]) -> dict:
        prefixes = DiagnosisEngine(self.s3, None, self.bucket, "").evidence_prefixes(
            incident, timeline
        )[:MAX_EVENTS]
        rows = []
        seen = set()
        for prefix in prefixes:
            body = self.s3.get_object(Bucket=self.bucket, Key=f"{prefix}/logs.json")["Body"]
            try:
                raw = body.read(MAX_OBJECT_BYTES + 1)
            finally:
                body.close()
            if len(raw) > MAX_OBJECT_BYTES:
                raise ValueError("saved log evidence is too large")
            document = json.loads(raw)
            for row in document.get("error_samples", []):
                if not isinstance(row, dict):
                    continue
                text = json.dumps(row, ensure_ascii=False)
                if any(
                    token in text.lower()
                    for token in ("chaos updated", "/chaos", "/api/simulation")
                ):
                    continue
                item = {key: str(value)[:500] for key, value in row.items() if key != "@ptr"}
                fingerprint = json.dumps(item, sort_keys=True)
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    rows.append(item)
        # Evidence may include a burst of older, unrelated errors. Keep the
        # newest excerpts visible when the detail page caps its displayed rows.
        rows.sort(key=lambda row: row.get("@timestamp", ""), reverse=True)
        return {"source": "saved CloudWatch error/warning excerpts", "rows": rows[:MAX_LOG_ROWS]}

    def search_logs(self, incident: Incident, search: str) -> dict:
        """Search the full bounded incident window, with the search term as a literal."""
        if not search.strip() or len(search) > 100 or any(ord(char) < 32 for char in search):
            raise ValueError("search must be 1-100 printable characters")
        if self.cloudwatch_logs is None:
            raise RuntimeError("CloudWatch Logs search is not configured")
        start, end = self.window(incident)
        groups = [f"{config.LOG_PREFIX}/{name}" for name in self.services(incident)]
        groups.append(f"{config.LOG_PREFIX}/backend")
        query = (
            "fields @timestamp, service, level, endpoint, status_code, error_type, "
            "error_message, message, trace_id | "
            + EXCLUDE_INJECTION
            + f" | filter @message like {json.dumps(search.strip())} "
            "| sort @timestamp desc | limit 200"
        )
        query_id = self.cloudwatch_logs.start_query(
            logGroupNames=groups,
            startTime=int(start.timestamp()),
            endTime=int(end.timestamp()),
            queryString=query,
            limit=200,
        )["queryId"]
        for _ in range(16):
            result = self.cloudwatch_logs.get_query_results(queryId=query_id)
            if result["status"] == "Complete":
                rows = [
                    {field["field"]: field["value"] for field in row if field["field"] != "@ptr"}
                    for row in result["results"]
                ]
                return {"source": "CloudWatch Logs search", "rows": rows}
            if result["status"] not in {"Running", "Scheduled"}:
                raise RuntimeError(f"CloudWatch Logs search ended with {result['status']}")
            time.sleep(0.5)
        raise RuntimeError("CloudWatch Logs search timed out")
