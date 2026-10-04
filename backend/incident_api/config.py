"""Settings, read from environment variables with local-development defaults."""

import os
from dataclasses import dataclass

from incident_api.dependency import GRAPH

TABLE_NAME = os.getenv("INCIDENT_TABLE", "incident-store")
LOG_PREFIX = os.getenv("INCIDENT_LOG_PREFIX", "/incident")
METRIC_NAMESPACE = os.getenv("INCIDENT_METRIC_NAMESPACE", "Incident")
ALARM_PREFIX = os.getenv("INCIDENT_ALARM_PREFIX", "incident")
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
# Named AWS profile for local development (e.g. "incident" from `aws login`). When unset,
# boto3 uses its default credential chain, e.g. the EC2 instance role in production.
AWS_PROFILE = os.getenv("INCIDENT_AWS_PROFILE") or None
QUEUE_URL = os.getenv("INCIDENT_QUEUE_URL") or None
EVIDENCE_BUCKET = os.getenv("INCIDENT_EVIDENCE_BUCKET") or None
DIAGNOSIS_PROVIDER = os.getenv("DIAGNOSIS_PROVIDER", "gemini").lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "openai.gpt-oss-20b-1:0")
HEALTH_INTERVAL_S = float(os.getenv("HEALTH_INTERVAL_S", "3"))
HEALTH_TIMEOUT_S = float(os.getenv("HEALTH_TIMEOUT_S", "2"))
INCIDENT_MODE = os.getenv("INCIDENT_MODE", "aws").lower()
LOCAL_DB_PATH = os.getenv("LOCAL_DB_PATH", "data/incidents.sqlite3")
LOCAL_FAILURE_DURATION_S = float(os.getenv("LOCAL_FAILURE_DURATION_S", "9"))


@dataclass(frozen=True)
class MonitoredService:
    name: str
    display_name: str
    url: str
    depends_on: tuple[str, ...]


# The simulated company. Defaults match the ports published by docker-compose.yml;
# inside compose the backend overrides them with container hostnames.
_SERVICE_URLS = {
    "gateway": ("GATEWAY_URL", "http://localhost:8090"),
    "order": ("ORDER_URL", "http://localhost:8091"),
    "payment": ("PAYMENT_URL", "http://localhost:8092"),
    "inventory": ("INVENTORY_URL", "http://localhost:8093"),
}
SERVICES = tuple(
    MonitoredService(
        node.name,
        node.display_name,
        os.getenv(*_SERVICE_URLS[node.name]),
        node.depends_on,
    )
    for node in GRAPH.nodes.values()
    if node.monitored
)
