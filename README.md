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

Every request is logged as one JSON line (`service`, `trace_id`, `endpoint`, `status_code`, `latency_ms`, `error_type`, ...). Send an `x-trace-id` header (or let the gateway generate one) to follow a request across services.

### Tests

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows; use .venv/bin/activate on Mac/Linux
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && pytest   # unit tests
pytest integration                                # against a running `docker compose up` stack
```

### Order consistency

If payment fails, the order service compensates in the background: it voids the payment and releases the reserved stock, retrying with backoff for about a minute. Both calls are idempotent on `order_id`:
- Payment has a unique `order_id`. A repeated charge returns the existing payment instead of charging twice. A void leaves a `voided` row, so a slow charge that lands afterwards is rejected (`409 PaymentVoided`).
- Inventory tracks reservations per `order_id`, so a retried reserve or release has no extra effect.

Full build plan: [docs/PLAN.md](docs/PLAN.md)
