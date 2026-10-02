import json

import boto3
import httpx
import pytest
from moto import mock_aws

from incident_api.setup_table import ensure_table
from incident_api.store import IncidentStore

REGION = "us-east-2"


@pytest.fixture
def store(monkeypatch):
    """An IncidentStore backed by moto's in-memory DynamoDB (never touches real AWS)."""
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        ensure_table(boto3.client("dynamodb", region_name=REGION), "test-incidents")
        table = boto3.resource("dynamodb", region_name=REGION).Table("test-incidents")
        yield IncidentStore(table)


class FakeServices:
    """Stands in for the simulated services' /health and /chaos endpoints.

    Each service is addressed by hostname, e.g. http://payment/health.
    """

    def __init__(self, names):
        self.down: set[str] = set()
        self.unhealthy: dict[str, str] = {}  # name -> error message
        self.chaos = {name: self._normal() for name in names}

    @staticmethod
    def _normal() -> dict:
        return {"latency_ms": 0, "error_rate": 0.0, "db_delay_s": 0.0}

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.host
        if name in self.down:
            raise httpx.ConnectError("connection refused")
        if request.url.path == "/health":
            if name in self.unhealthy:
                return httpx.Response(503, json={"message": self.unhealthy[name]})
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/chaos":
            if request.method == "POST":
                body = json.loads(request.content)
                if body.pop("crash", False):
                    self.down.add(name)
                self.chaos[name].update(body)
            elif request.method == "DELETE":
                self.chaos[name] = self._normal()
            return httpx.Response(200, json=self.chaos[name])
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake():
    """Fake payment + order services, addressed as http://payment and http://order."""
    return FakeServices(["payment", "order"])
