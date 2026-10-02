"""Failure scenarios end to end, against the running compose stack.

Payment's queries are slowed to 3 s, so its 5-connection pool runs out under concurrent
load. That must surface as PoolTimeout in payment and as timeouts/errors in order and the
gateway, and every order whose outcome was unknown must resolve to the truth afterwards.

The load generator is paused during this module so its traffic doesn't skew the results.
"""

import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:8090")
ORDER = os.getenv("ORDER_URL", "http://127.0.0.1:8091")
PAYMENT = os.getenv("PAYMENT_URL", "http://127.0.0.1:8092")


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def http():
    # No connection reuse: these tests stop and restart containers, and a pooled connection
    # to a container that no longer exists can hang behind Docker's port forwarding.
    limits = httpx.Limits(max_keepalive_connections=0)
    with httpx.Client(timeout=15, limits=limits) as client:
        yield client


@pytest.fixture(scope="module", autouse=True)
def paused_loadgen():
    compose("stop", "loadgen")
    yield
    compose("start", "loadgen")


def wait_until(condition, what: str, timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except httpx.HTTPError:
            pass  # e.g. the service is still starting
        time.sleep(0.5)
    pytest.fail(f"timed out waiting for: {what}")


def healthy(http, url: str) -> bool:
    return http.get(f"{url}/health").status_code == 200


def wait_for_working_orders(http) -> None:
    """Start every scenario from a healthy system. After a container restart, the first
    request over a service's stale pooled connection can fail, as it would in production."""
    wait_until(
        lambda: http.post(f"{GATEWAY}/orders", json=order_body()).status_code == 201,
        "orders to succeed end to end",
    )


@pytest.fixture(autouse=True)
def healthy_system(http):
    wait_for_working_orders(http)


@pytest.fixture
def slow_database(http):
    http.post(f"{PAYMENT}/chaos", json={"db_delay_s": 3}).raise_for_status()
    yield
    http.delete(f"{PAYMENT}/chaos").raise_for_status()
    wait_until(lambda: healthy(http, PAYMENT), "payment healthy")


def concurrently(n: int, fn) -> list:
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(fn, range(n)))


def error_types(responses: list[httpx.Response]) -> set[str]:
    return {r.json().get("error") for r in responses if r.status_code >= 400}


def outcomes(responses: list[httpx.Response]) -> list[tuple[int, str | None]]:
    return sorted((r.status_code, r.json().get("error")) for r in responses)


def order_body(order_id: str | None = None) -> dict:
    return {"item_id": "sku-3", "quantity": 1, **({"order_id": order_id} if order_id else {})}


def test_concurrent_payments_exhaust_the_pool(http, slow_database):
    responses = concurrently(
        15,
        lambda _: http.post(
            f"{PAYMENT}/payments", json={"order_id": f"it-{uuid.uuid4().hex}", "amount": 1.0}
        ),
    )
    assert 201 in [r.status_code for r in responses], "the first five get a connection"
    assert error_types(responses) == {"PoolTimeout"}
    pool_errors = [r.json()["message"] for r in responses if r.status_code == 503]
    assert all("connection pool exhausted" in message for message in pool_errors)


def test_failure_cascades_through_order_and_gateway(http, slow_database):
    from_order = concurrently(10, lambda _: http.post(f"{ORDER}/orders", json=order_body()))
    assert all(r.status_code in (502, 504) for r in from_order), outcomes(from_order)
    assert error_types(from_order) <= {"DependencyTimeout", "DependencyError"}

    from_gateway = concurrently(10, lambda _: http.post(f"{GATEWAY}/orders", json=order_body()))
    assert all(r.status_code == 502 for r in from_gateway), outcomes(from_gateway)
    assert error_types(from_gateway) == {"DependencyError"}


def test_timed_out_orders_resolve_to_the_truth(http):
    """A timed-out order may still have been charged. Afterwards every such order must be
    either confirmed (its charge landed late) or not found (it never was), matching payment,
    even after the order service restarts and forgets what it saw."""
    order_ids = [f"it-{uuid.uuid4().hex[:12]}" for _ in range(8)]

    http.post(f"{PAYMENT}/chaos", json={"db_delay_s": 3}).raise_for_status()
    concurrently(8, lambda i: http.post(f"{GATEWAY}/orders", json=order_body(order_ids[i])))
    http.delete(f"{PAYMENT}/chaos").raise_for_status()
    wait_until(lambda: healthy(http, PAYMENT), "payment healthy")

    def check_against_payment() -> set[int]:
        outcomes = set()
        for order_id in order_ids:
            truth = http.get(f"{PAYMENT}/payments/by-order/{order_id}")
            order = http.get(f"{GATEWAY}/orders/{order_id}")
            assert order.status_code == truth.status_code, order_id
            if truth.status_code == 200:
                assert order.json()["status"] == "confirmed"
                assert order.json()["payment_id"] == truth.json()["payment_id"]
            outcomes.add(truth.status_code)
        return outcomes

    # 5 charges held a connection and landed after the caller gave up; 3 never got one.
    assert check_against_payment() == {200, 404}

    compose("restart", "order")
    wait_until(lambda: healthy(http, ORDER), "order healthy after restart")
    assert check_against_payment() == {200, 404}


def test_orders_during_a_payment_outage_leave_nothing_behind(http):
    order_ids = [f"it-{uuid.uuid4().hex[:12]}" for _ in range(3)]
    compose("stop", "payment")
    try:
        # 502, or 504 when the lookup of the stopped container outlasts the gateway timeout.
        for order_id in order_ids:
            response = http.post(f"{GATEWAY}/orders", json=order_body(order_id))
            assert response.status_code in (502, 504), outcomes([response])
    finally:
        compose("start", "payment")
    wait_until(lambda: healthy(http, PAYMENT), "payment healthy after restart")

    wait_for_working_orders(http)
    for order_id in order_ids:
        assert http.get(f"{GATEWAY}/orders/{order_id}").status_code == 404
