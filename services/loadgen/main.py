"""Load generator: steady fake customer traffic through the gateway.

Without traffic there are no latencies or error rates to watch, so this runs
alongside the services and prints a JSON summary every REPORT_EVERY seconds.
"""

import json
import os
import random
import signal
import statistics
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8090")
RATE = float(os.getenv("RATE", "5"))  # requests per second
REPORT_EVERY = float(os.getenv("REPORT_EVERY", "10"))  # seconds
ITEMS = ["sku-1", "sku-2", "sku-3", "sku-4", "sku-5"]


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self._outcomes: Counter[str] = Counter()
        self._latencies: list[float] = []

    def record(self, outcome: str, latency_ms: float) -> None:
        with self._lock:
            self._outcomes[outcome] += 1
            self._latencies.append(latency_ms)

    def flush(self) -> tuple[Counter[str], list[float]]:
        with self._lock:
            outcomes, latencies = self._outcomes, self._latencies
            self._outcomes, self._latencies = Counter(), []
        return outcomes, latencies


def summarize(outcomes: Counter[str], latencies: list[float]) -> dict:
    total = sum(outcomes.values())
    ok = sum(n for outcome, n in outcomes.items() if outcome.startswith("2"))
    summary = {
        "requests": total,
        "success_rate": round(ok / total, 3) if total else None,
        "by_outcome": dict(sorted(outcomes.items())),
        "p50_ms": None,
        "p95_ms": None,
    }
    if len(latencies) >= 2:
        cuts = statistics.quantiles(latencies, n=20)  # 19 cut points: 5%, 10%, ... 95%
        summary["p50_ms"] = round(cuts[9], 1)
        summary["p95_ms"] = round(cuts[18], 1)
    elif latencies:
        summary["p50_ms"] = summary["p95_ms"] = round(latencies[0], 1)
    return summary


def send_one(client: httpx.Client, stats: Stats, recent_orders: deque) -> None:
    start = time.perf_counter()
    try:
        if recent_orders and random.random() < 0.2:
            # Index instead of iterating: other threads append concurrently, and the
            # deque's length never shrinks, so any index below len() stays valid.
            order_id = recent_orders[random.randrange(len(recent_orders))]
            response = client.get(f"/orders/{order_id}")
        else:
            body = {"item_id": random.choice(ITEMS), "quantity": random.randint(1, 3)}
            response = client.post("/orders", json=body)
            if response.status_code == 201:
                recent_orders.append(response.json()["order_id"])
        outcome = str(response.status_code)
    except httpx.HTTPError as exc:
        outcome = type(exc).__name__
    stats.record(outcome, (time.perf_counter() - start) * 1000)


def main() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())  # let `docker compose stop` exit fast

    client = httpx.Client(base_url=GATEWAY_URL, timeout=10)
    stats = Stats()
    recent_orders: deque[str] = deque(maxlen=200)
    executor = ThreadPoolExecutor(max_workers=64)
    next_send = next_report = time.monotonic()
    next_report += REPORT_EVERY

    while not stop.is_set():
        executor.submit(send_one, client, stats, recent_orders)
        next_send += 1 / RATE
        now = time.monotonic()
        if now >= next_report:
            entry = {
                "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "service": "loadgen",
                "message": "traffic summary",
                **summarize(*stats.flush()),
            }
            print(json.dumps(entry), flush=True)
            next_report += REPORT_EVERY
        stop.wait(max(0.0, next_send - now))

    executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
