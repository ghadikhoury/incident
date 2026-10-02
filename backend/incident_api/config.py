"""Settings, read from environment variables with local-development defaults."""

import os
from dataclasses import dataclass

TABLE_NAME = os.getenv("INCIDENT_TABLE", "incident-store")
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
# Named AWS profile for local development (e.g. "incident" from `aws login`). When unset,
# boto3 uses its default credential chain, e.g. the EC2 instance role in production.
AWS_PROFILE = os.getenv("INCIDENT_AWS_PROFILE") or None
HEALTH_INTERVAL_S = float(os.getenv("HEALTH_INTERVAL_S", "3"))
HEALTH_TIMEOUT_S = float(os.getenv("HEALTH_TIMEOUT_S", "2"))


@dataclass(frozen=True)
class MonitoredService:
    name: str
    display_name: str
    url: str
    depends_on: tuple[str, ...]


# The simulated company. Defaults match the ports published by docker-compose.yml;
# inside compose the backend overrides them with container hostnames.
SERVICES = (
    MonitoredService(
        "gateway", "Gateway", os.getenv("GATEWAY_URL", "http://localhost:8090"), ("order",)
    ),
    MonitoredService(
        "order",
        "Orders",
        os.getenv("ORDER_URL", "http://localhost:8091"),
        ("inventory", "payment"),
    ),
    MonitoredService(
        "payment", "Payments", os.getenv("PAYMENT_URL", "http://localhost:8092"), ("postgres",)
    ),
    MonitoredService(
        "inventory", "Inventory", os.getenv("INVENTORY_URL", "http://localhost:8093"), ()
    ),
)
