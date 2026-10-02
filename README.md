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

Full build plan: [docs/PLAN.md](docs/PLAN.md)
