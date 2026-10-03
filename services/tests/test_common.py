import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

import common.http
from common.app import create_app
from common.errors import ServiceError
from common.logs import JsonFormatter

app = create_app("test-service")


@app.get("/ok")
def ok():
    return {"ok": True}


@app.get("/fail")
def fail():
    raise ServiceError(503, "SomethingBroke", "it broke")


@app.get("/crash")
def crash():
    raise RuntimeError("boom")


@app.get("/items/{item_id}")
def item(item_id: str):
    return {"item_id": item_id}


client = TestClient(app)


def request_logs(caplog):
    return [r for r in caplog.records if r.getMessage() == "request"]


def test_trace_id_is_reused_when_provided():
    response = client.get("/ok", headers={"x-trace-id": "abc123"})
    assert response.headers["x-trace-id"] == "abc123"


def test_trace_id_is_generated_when_missing():
    response = client.get("/ok")
    assert len(response.headers["x-trace-id"]) == 32


def test_service_error_becomes_json_response_and_is_logged(caplog):
    with caplog.at_level(logging.INFO, logger="incident"):
        response = client.get("/fail")
    assert response.status_code == 503
    assert response.json() == {"error": "SomethingBroke", "message": "it broke"}
    fields = request_logs(caplog)[-1].fields
    assert fields["status_code"] == 503
    assert fields["error_type"] == "SomethingBroke"
    assert fields["error_message"] == "it broke"


def test_unhandled_exception_becomes_500(caplog):
    with caplog.at_level(logging.INFO, logger="incident"):
        response = client.get("/crash")
    assert response.status_code == 500
    assert response.json()["error"] == "InternalServerError"
    assert request_logs(caplog)[-1].fields["status_code"] == 500


def test_request_log_uses_route_template(caplog):
    with caplog.at_level(logging.INFO, logger="incident"):
        client.get("/items/42")
    fields = request_logs(caplog)[-1].fields
    assert fields["endpoint"] == "/items/{item_id}"
    assert fields["method"] == "GET"
    assert fields["latency_ms"] >= 0


def test_json_formatter_outputs_one_json_object():
    record = logging.LogRecord("incident", logging.INFO, "", 0, "request", None, None)
    record.fields = {"status_code": 200}
    entry = json.loads(JsonFormatter("payment").format(record))
    assert entry["service"] == "payment"
    assert entry["status_code"] == 200
    assert entry["message"] == "request"


@pytest.fixture
def fake_downstream(monkeypatch):
    """Replace the shared HTTP client with one whose responses we control."""

    def use(handler):
        monkeypatch.setattr(
            common.http, "_client", httpx.Client(transport=httpx.MockTransport(handler))
        )

    return use


def test_call_forwards_trace_id(fake_downstream):
    seen = {}

    def handler(request):
        seen["trace"] = request.headers.get("x-trace-id")
        return httpx.Response(200, json={})

    fake_downstream(handler)
    proxy = create_app("proxy")

    @proxy.get("/proxy")
    def do_proxy():
        common.http.call("GET", "http://downstream/x", dependency="downstream", timeout=1)
        return {}

    TestClient(proxy).get("/proxy", headers={"x-trace-id": "trace-1"})
    assert seen["trace"] == "trace-1"


@pytest.mark.parametrize(
    ("handler_result", "status", "error_type"),
    [
        (httpx.ReadTimeout("slow"), 504, "DependencyTimeout"),
        (httpx.ConnectError("refused"), 503, "DependencyUnavailable"),
        (httpx.Response(500), 502, "DependencyError"),
    ],
)
def test_call_maps_failures_to_service_errors(fake_downstream, handler_result, status, error_type):
    def handler(request):
        if isinstance(handler_result, Exception):
            raise handler_result
        return handler_result

    fake_downstream(handler)
    with pytest.raises(ServiceError) as exc_info:
        common.http.call("GET", "http://downstream/x", dependency="downstream", timeout=1)
    assert exc_info.value.status_code == status
    assert exc_info.value.error_type == error_type


def test_call_returns_4xx_to_caller(fake_downstream):
    fake_downstream(lambda request: httpx.Response(404, json={"error": "NotFound"}))
    response = common.http.call("GET", "http://downstream/x", dependency="downstream", timeout=1)
    assert response.status_code == 404


def emf_fields(caplog) -> dict:
    return request_logs(caplog)[-1].fields


@pytest.mark.parametrize(
    ("path", "errors"), [("/ok", 0), ("/nope", 0), ("/fail", 1), ("/crash", 1)]
)
def test_request_logs_carry_cloudwatch_metrics(caplog, path, errors):
    """Each request line is valid Embedded Metric Format: CloudWatch turns it into metrics."""
    with caplog.at_level(logging.INFO, logger="incident"):
        client.get(path)
    fields = emf_fields(caplog)
    [directive] = fields["_aws"]["CloudWatchMetrics"]
    assert directive["Namespace"] == "Incident"
    assert directive["Dimensions"] == [["Service"]]
    assert isinstance(fields["_aws"]["Timestamp"], int)
    # Every declared metric and dimension must be a top-level field with a value.
    for metric in directive["Metrics"]:
        assert isinstance(fields[metric["Name"]], int | float), metric["Name"]
    assert fields["Service"] == "test-service"
    assert fields["Requests"] == 1
    assert fields["Errors"] == errors  # 5xx only: a 404 is the caller's mistake
    assert fields["Latency"] == fields["latency_ms"]


def test_emf_survives_json_formatting(caplog):
    with caplog.at_level(logging.INFO, logger="incident"):
        client.get("/ok")
    line = json.loads(JsonFormatter("test-service").format(request_logs(caplog)[-1]))
    assert line["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Name"] == "Latency"
    assert line["Latency"] >= 0


def test_cpu_sample_is_an_emf_metric():
    from common.metrics import cpu_utilization_line

    line = cpu_utilization_line("payment", 87.35)
    assert line["Service"] == "payment"
    assert line["CpuUtilizationPct"] == 87.3
    assert line["_aws"]["CloudWatchMetrics"][0]["Metrics"] == [
        {"Name": "CpuUtilizationPct", "Unit": "Percent"}
    ]


def test_chaos_controls_are_logged_but_not_metered(caplog):
    with caplog.at_level(logging.INFO, logger="incident"):
        client.get("/chaos")
    fields = emf_fields(caplog)
    assert fields["endpoint"] == "/chaos"
    assert "_aws" not in fields
