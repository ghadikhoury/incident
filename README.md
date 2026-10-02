# Incident

Incident detection, diagnosis and response for distributed systems.

Incident watches a set of microservices, detects failures through AWS CloudWatch, groups related alarms into a single incident, works out which service failed first, and uses an LLM (through Amazon Bedrock) to suggest a root cause and remediation. An engineer approves or rejects each suggestion before anything runs.

> Status: early development. See the roadmap below.

## Architecture (target)

```
services (Docker on EC2) ──logs/metrics──▶ CloudWatch ──alarm──▶ EventBridge ──▶ Lambda
                                                                                 │
                                       DynamoDB (incidents) ◀────────────────────┤
                                       S3 (evidence)        ◀────────────────────┤
                                       SQS ◀─────────────────────────────────────┘
                                        │
                              Incident backend (FastAPI) ──WebSocket──▶ Dashboard (React)
                                        │
                                     Bedrock (diagnosis)
```

## Roadmap

1. [x] Project setup and AWS account
2. [ ] Simulated microservices + failure injection (local)
3. [ ] Incident backend + live dashboard
4. [ ] Deploy to EC2
5. [ ] CloudWatch detection
6. [ ] Lambda incident pipeline (DynamoDB, S3, SQS)
7. [ ] Dependency graph, correlation, severity
8. [ ] AI diagnosis (Bedrock) + human approval
9. [ ] Incident detail page + evaluation harness
10. [ ] Infrastructure as code (CDK), hardening, docs

## Development

Requirements: Python 3.12, Node 20+, Docker Desktop, AWS CLI v2.

```bash
cp .env.example .env   # then fill in values
```

### Run the simulated system

```bash
docker compose up -d --build --wait     # start everything, wait until healthy
curl -X POST localhost:8090/orders -H 'content-type: application/json' \
     -d '{"item_id":"sku-1","quantity":2}'
docker compose logs -f order payment    # watch the JSON logs
docker compose down                     # stop (add -v to also wipe the database)
```

| Service | Local port | Depends on |
|---|---|---|
| gateway | 8090 | order |
| order | 8091 | inventory, payment |
| payment | 8092 | postgres |
| inventory | 8093 | none |

`loadgen` sends about 5 requests/second through the gateway and prints a traffic summary every 10 s (`docker compose logs -f loadgen`).

### Break things on purpose

Every service has a `/chaos` endpoint. Settings combine; `DELETE /chaos` restores normal behaviour.

| Failure | Command | What happens |
|---|---|---|
| Slow database | `curl -X POST localhost:8092/chaos -H 'content-type: application/json' -d '{"db_delay_s": 3}'` | Payment queries hold their connections for 3 s, the 5-connection pool runs out (`PoolTimeout`), order times out (`DependencyTimeout`), the gateway returns 502s |
| Latency | `... localhost:8092/chaos ... -d '{"latency_ms": 3000}'` | Every payment request takes 3 s longer |
| Error rate | `... localhost:8093/chaos ... -d '{"error_rate": 0.4}'` | 40% of inventory requests fail with HTTP 500 |
| Crash | `... localhost:8092/chaos ... -d '{"crash": true}'` | Payment exits; order gets `DependencyUnavailable`. Restart with `docker compose start payment` |
| Recover | `curl -X DELETE localhost:8092/chaos` | Back to normal |

Ports: gateway 8090, order 8091, payment 8092, inventory 8093. Chaos changes are logged as `"message": "chaos updated"`, so they can be excluded from the evidence Incident analyses.

Every request is logged as one JSON line (`service`, `trace_id`, `endpoint`, `status_code`, `latency_ms`, `error_type`, ...). Send an `x-trace-id` header (or let the gateway generate one) to follow a request across services.

### Incident backend

The backend (`backend/incident_api`, port 8000) stores incidents in DynamoDB, polls every service's health every 3 s, and pushes changes to dashboards over a WebSocket.

Log in (`aws login --profile incident`) and create or upgrade the table:

```bash
cd backend && INCIDENT_AWS_PROFILE=incident python -m incident_api.setup_table
# PowerShell: cd backend; $env:INCIDENT_AWS_PROFILE="incident"; python -m incident_api.setup_table
```

Run the setup command again when upgrading an existing table. It adds missing indexes and
backfills preexisting OPEN and ACKNOWLEDGED incidents into the active index. The backfill
is safe to rerun after an interruption and does not add resolved incidents.

`docker compose up` runs the backend with your `~/.aws` folder mounted, using the profile named by `AWS_PROFILE` in `.env`. If AWS calls start failing, your 12-hour login expired: run `aws login --profile incident` again.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/services` | Latest health of every service (`healthy` / `unhealthy` / `down`), dependencies, active chaos |
| GET | `/api/incidents?active=true` | Incidents, newest first (`active=true` hides resolved) |
| POST | `/api/incidents` | Declare an incident: `{"title", "service", "severity", "summary"?, "actor"?}` |
| GET | `/api/incidents/{id}` | Incident with its timeline |
| PATCH | `/api/incidents/{id}` | Change `severity`, `status`, `assigned_to`, `title` |
| POST | `/api/incidents/{id}/acknowledge` | OPEN → ACKNOWLEDGED (`{"actor"?, "note"?}`) |
| POST | `/api/incidents/{id}/resolve` | → RESOLVED (final) |
| POST | `/api/simulation/failure` | `{"service": "payment", "failure": "db_slow" \| "latency" \| "error_rate" \| "crash"}` |
| POST | `/api/simulation/recover` | `{"service": "payment"}` clears injected failures |
| WS | `/api/ws` | Pushes `{"type": "services", ...}` and `{"type": "incident", ...}` messages |

Interactive API docs: http://localhost:8000/docs

### Tests

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows; use .venv/bin/activate on Mac/Linux
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && pytest   # unit tests
pytest integration                                # against a running `docker compose up` stack
```

### Order consistency

An order exists exactly when payment has captured a charge for its `order_id`. Nothing else holds state that a failure could leave half-done: inventory only quotes prices.

- **Idempotent orders.** Clients may send their own `order_id`. Payment durably records the item, quantity, and amount with the charge. Retrying the same order never charges twice: the retry returns the original order with `200`, and reusing an id for a different item, quantity, or amount is a `409 IdempotencyConflict`, even if two orders have the same total.
- **Unknown outcomes resolve themselves.** A request that times out may still have been charged (the payment was being written when the caller gave up). `GET /orders/{order_id}` asks payment for the truth and retrieves the original item and quantity after an order-service restart, so such an order appears as `confirmed` once its charge has landed and is a `404` if it never was. The payment schema upgrade adds nullable identity columns to existing tables; charges recorded before this upgrade cannot recover item and quantity that were never stored.

Full build plan: [docs/PLAN.md](docs/PLAN.md)
