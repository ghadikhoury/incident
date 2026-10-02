from collections import Counter, deque

import httpx

from loadgen.main import Stats, send_one, summarize


def test_summarize_counts_success_rate_and_percentiles():
    summary = summarize(Counter({"201": 8, "504": 2}), [float(i) for i in range(1, 101)])
    assert summary["requests"] == 10
    assert summary["success_rate"] == 0.8
    assert summary["by_outcome"] == {"201": 8, "504": 2}
    assert 49 <= summary["p50_ms"] <= 51
    assert 94 <= summary["p95_ms"] <= 96


def test_summarize_handles_no_traffic():
    summary = summarize(Counter(), [])
    assert summary["requests"] == 0
    assert summary["success_rate"] is None


def test_send_one_records_connection_errors():
    def refuse(request):
        raise httpx.ConnectError("refused")

    client = httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(refuse))
    stats = Stats()
    send_one(client, stats, deque())
    outcomes, latencies = stats.flush()
    assert outcomes == Counter({"ConnectError": 1})
    assert len(latencies) == 1


def test_send_one_remembers_created_orders():
    client = httpx.Client(
        base_url="http://gateway",
        transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"order_id": "abc"})),
    )
    recent = deque()
    send_one(client, Stats(), recent)
    assert list(recent) == ["abc"]
