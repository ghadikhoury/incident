# Incident: Stage 1 Build Plan

## Context
Incident is meant to be a genuine, usable product (startup-style), built on AWS (EC2, S3, Lambda and related services), that **actually works end to end**. This plan covers **Stage 1 only**. Everything agentic from Stage 2 (RAG, LangChain, LangGraph, MCP, agents) is deferred. Within Stage 1 we get one failure flowing through the *entire* pipeline as early as possible, then add breadth. After Step 6 there is always a working demo.

- **Repo:** `C:\Users\khour\Projects\incident` (GitHub `ghadikhoury/incident`).
- **LLM:** Amazon Bedrock with the model left open (Claude or an OpenAI model on Bedrock). We use Bedrock's **Converse API**, which works the same for every model, so switching models means changing one config value.
- **AWS account:** uses AWS's **new project-based experience** (launched Sept 2026) on the **Free Plan** ($100 credits; no charges possible until upgrading to the Paid Plan; the account closes when credits run out or after 6 months). See "AWS account notes" below.

---

## Where we are (update as we go)

**Step 1: done ✅**
- [x] Python 3.12 (added to PATH), AWS CLI v2.37.8, Docker Desktop 4.93.0 installed
- [x] Repo skeleton + CI (PR #1): https://github.com/ghadikhoury/incident/pull/1
- [x] Installed WSL 2, Docker Desktop running, `docker run hello-world` works
- [x] AWS project **Incident** created (us-east-2)
- [x] MFA turned on in AWS Settings
- [x] `aws login --profile incident` works (sessions last 12 h; re-run the command when it expires)
- [x] Budget `incident-monthly-20`: $20/month, counts usage *before* credits, emails at 50/80/100% actual + 100% forecast
- [x] PR #1 merged, branch protection on `main` (CI must pass)

**Step 2: done ✅** (PRs #2, #3): simulated shop, structured logs + trace ids, `/chaos` failure injection, load generator, integration tests for the slow-database and outage scenarios.

**Step 3: done ✅** (PRs #4, #5): Incident backend on DynamoDB (`incident-store`, transactional updates, sparse active index), health monitor, WebSocket, React dashboard.

**Step 4: in review**: EC2 deployment, see [DEPLOY.md](DEPLOY.md).
- [x] Instance `incident` (t3.small, Ubuntu 24.04) running the full stack; only SSH is open (from your IP), the dashboard is reached through an SSH tunnel
- [x] IAM role `incident-ec2`: DynamoDB `incident-store` only (verified: everything else is denied)
- [x] Survives stop/start (boot service); **stop it when not in use**

---

## What everything is (plain-English glossary)

**AWS services**
| Name | What it is | What Incident uses it for |
|---|---|---|
| **EC2** (Elastic Compute Cloud) | A Linux computer you rent in AWS's data center, by the hour. You SSH into it like any server. | Runs all our Docker containers (the fake company's services + Incident backend + dashboard). |
| **S3** (Simple Storage Service) | Unlimited file storage. You put files ("objects") into "buckets" under paths ("keys"). Cheap and durable, not a database. | Stores raw evidence per incident: `incidents/INC-1042/payment.log`, `metrics.json`, `llm-input.json`. |
| **Lambda** | You upload one function and AWS runs it *only when an event happens*. No server to manage; you pay per run (essentially free at our scale). | Runs when an alarm fires: creates or updates the incident, gathers logs into S3, notifies the backend. |
| **CloudWatch** | AWS's monitoring system. **Logs** = stored log lines. **Metrics** = numbers over time (latency, error rate). **Alarms** = rules like "latency > 2s for 2 minutes → ALARM". | How Incident *detects* problems. Our services' logs go here and metrics are pulled out of them. |
| **EventBridge** | An AWS "event router": "when event X happens, send it to target Y." | Routes "alarm changed to ALARM" events to the Lambda. |
| **DynamoDB** | AWS's fast NoSQL database. Data is stored as items (JSON-like rows) found by key. | Structured incident state: id, status, severity, timeline, alerts. |
| **SQS** (Simple Queue Service) | A message queue. One side drops messages in, the other side picks them up when ready. Nothing is lost if the receiver is down. | Lambda drops "incident updated" messages; the backend picks them up and pushes them to the dashboard. |
| **IAM** (Identity & Access Management) | AWS's permission system. *Roles* say exactly which actions a piece of software may do. | Least privilege: the Lambda may write *only* to our table and bucket; EC2 may read *only* what the backend needs. |
| **Bedrock** | AWS's service for calling AI models (Claude, OpenAI open models, Llama, etc.) through AWS, authorized by IAM. | Generates the root-cause diagnosis. |
| **CDK** (Cloud Development Kit) | Amazon's "infrastructure as code": Python code that creates all of the AWS resources above. | Step 10: rebuild the whole AWS setup with one command. |

**Everything else**
| Name | What it is |
|---|---|
| **Docker** | Packages a program plus everything it needs into a "container" that runs the same everywhere (your laptop, EC2). |
| **Docker Compose** | One file (`docker-compose.yml`) that starts many containers together and networks them; `docker compose up` launches the whole fake company. |
| **Postgres** | A real SQL database, running in a container. Payment Service uses it, so we can cause *real* connection-pool exhaustion. |
| **FastAPI** | A Python web framework for building HTTP APIs. Used for every microservice and the Incident backend. |
| **WebSocket** | A connection that stays open so the server can *push* updates to the browser instantly (no refresh). |
| **React + TypeScript (Vite)** | The dashboard. React = UI library, TypeScript = JavaScript with types, Vite = build tool. **Recharts** for graphs. |
| **Structured logs / EMF** | Logs written as JSON instead of prose. CloudWatch's *Embedded Metric Format* (EMF) lets one JSON log line also count as a metric, so there is no separate metrics pipeline. |
| **trace_id** | An ID created at the gateway and passed along in a header to every service, so one user request can be followed across all of them. |
| **GitHub Actions (CI)** | GitHub runs our tests automatically on every pull request. |

---

## AWS account notes (new project-based experience)
- **Projects:** each project is its own isolated AWS account. Ours is named **incident**. Code inside a project can reach that project's resources by default.
- **No root user and no IAM users.** MFA is managed at https://settings.aws.com. The CLI uses temporary credentials: `aws login --profile incident` (12-hour sessions, renewable for 90 days).
- **Region is fixed to `us-east-2` (Ohio)** and can't be changed.
- **Free Plan:** $100 credits, no charges possible. Project spend limits need the Paid Plan; until then the $20 Budget is an early-warning email. **Upgrade to the Paid Plan before real users depend on it**, because the Free Plan account closes when credits run out or after 6 months.
- Every service we need is supported (EC2, Lambda, S3, DynamoDB, CloudWatch, EventBridge, SQS, IAM, CloudFormation/CDK, Bedrock). **Bedrock cross-Region inference is NOT supported**, so in Step 8 we must pick a model that runs directly in us-east-2.
- Docs: https://docs.aws.amazon.com/accounts/latest/reference/sign-up-for-aws.html

---

## Architecture (end of Stage 1)

```
           ┌──────────────────────── EC2 instance (Docker Compose) ─────────────────────────┐
 load-gen ─▶ gateway ─▶ order ─▶ payment ─▶ postgres        incident-backend ◀──▶ dashboard (React)
           │                  └─▶ inventory                    │  ▲   WebSocket push        │
           └──── JSON/EMF logs ───────────────┬───────────────┼──┼────────────────────────┘
                                              ▼                │  │ long-polls SQS
                                CloudWatch Logs + Metrics      │  │
                                              ▼                │  │
                                      CloudWatch Alarm         │  │
                                              ▼                │  │
                                        EventBridge            │  │
                                              ▼                ▼  │
                                     Lambda (incident-processor) ─┼──▶ DynamoDB (incident-store)
                                              │                   ├──▶ S3 (evidence)
                                              └──────────────────▶ SQS ──┘
                     backend ──▶ Bedrock (diagnosis)   backend ──▶ simulator (approved remediation)
```

---

## Repo layout
```
incident/
├── services/            one Dockerfile; gateway/ order/ payment/ inventory/ (each: app.py)
│   ├── common/          shared logging + trace-id + failure injection (/chaos)
│   ├── loadgen/         steady fake traffic so metrics always have data
│   └── tests/
├── backend/             Incident FastAPI app (api/, correlation/, severity/, diagnosis/, ws/)
├── lambdas/incident_processor/
├── frontend/            React + TS dashboard
├── infra/               AWS CDK app (Step 10)
├── scenarios/           evaluation harness (Step 9)
├── docs/PLAN.md         this file
├── docker-compose.yml
└── .github/workflows/ci.yml
```

---

## The 10 steps

Each step = one branch + one PR. For each step Claude explains what we're doing, we write the code together, you run it, and you should be able to explain it before we merge. **You** do anything involving your AWS account, passwords or billing, with Claude guiding you.

### Step 1: Setup and AWS account safety
- Install Python 3.12, Docker Desktop (with WSL2), AWS CLI v2. Later: AWS CDK via npm.
- AWS: create the **incident** project, turn on MFA, `aws login --profile incident`, $20 budget alert.
- Repo skeleton, `.gitignore` (never commit `.env` or AWS keys), README, GitHub Actions CI (lint + pytest), branch protection on `main`.
- **Done when:** `aws sts get-caller-identity --profile incident` works, `docker run hello-world` works, and the first PR passes CI and is merged.

### Step 2: The fake company, running locally
- 4 FastAPI services (gateway → order → payment → postgres, order → inventory) + Postgres + load generator in `docker-compose.yml` with health checks.
- Shared `common` module: JSON logs with `timestamp, service, trace_id, endpoint, status_code, latency_ms, error_type, error_message`, and a `X-Trace-Id` header passed along on every call.
- **Failure injection** via `POST /chaos` on each service. Failures are *real* where possible:
  - `db_delay_s`: Payment's queries call `pg_sleep`, so its small connection pool (size 5) actually runs out and you get real "pool exhausted" errors that cascade to Order.
  - `latency_ms`, `error_rate` (e.g. 40% 500s), and `crash` (process exits; `docker compose start` recovers).
- **Done when:** `docker compose up` shows steady traffic, and turning on `db_slow` makes payment and then order logs fill with timeouts.

### Step 3: Incident backend + live dashboard (local, real DynamoDB)
- Create the DynamoDB table `incident-store` (on-demand billing, about $0). This is your first AWS resource, made by hand in the console so you see what it is.
- FastAPI backend: `GET/PATCH /incidents`, `/acknowledge`, `/resolve`, `/timeline`, `GET /services` (health from polling each service's `/health`), `POST /simulation/failure|recover`, and a `/ws` WebSocket.
- React dashboard: system-health banner, service list (green/yellow/red), live incidents table, and a demo panel with failure buttons.
- **Done when:** clicking "Inject db_slow" turns Payment red within seconds, and a manually created incident appears on the dashboard without refreshing.

### Step 4: Deploy to EC2
- Launch a `t3.small` Ubuntu instance (Free-Plan accounts can only launch Free Tier-eligible types; `t3.medium` isn't one). Learn **security groups** (firewall: only SSH, only from your IP), key pairs, SSH (including an SSH tunnel to reach the dashboard, which has no login yet), and installing Docker on Linux.
- Attach an **IAM instance role** to the instance, so it gets AWS permissions without any keys stored on the server.
- `git clone` and `docker compose up -d` on EC2.
- Habit: **stop the instance when you're done working** (about $0.02/hr while running, paid from credits).
- **Done when:** the dashboard loads from the EC2 public address in your browser.

### Step 5: CloudWatch detection
- Configure Docker's `awslogs` log driver so every container's logs go to CloudWatch Logs (`/incident/payment`, etc.).
- Services emit **EMF** lines, so CloudWatch automatically creates `Latency`, `Errors` and `Requests` metrics per service.
- Alarms: payment p90 latency > 2000 ms (2 of 3 one-minute periods), 5xx rate > 20%, health-check failures, plus the same for order.
- Note: CloudWatch alarms take **1–3 minutes** to fire. That's normal, and the demo is designed around it.
- **Done when:** injecting `db_slow` flips the payment alarm to `ALARM` in the console.

### Step 6: Lambda pipeline (walking skeleton complete)
- EventBridge rule: "CloudWatch alarm changed to ALARM" → Lambda `incident-processor` (Python).
- The Lambda:
  1. reads the alarm
  2. creates *or attaches to* an open incident using a DynamoDB **conditional write**, so 5 simultaneous alarms can't create 5 incidents
  3. runs a CloudWatch Logs Insights query for that ±5-minute window and writes the logs and metrics to `s3://incident-evidence-<acct>/incidents/INC-xxxx/`
  4. appends to the incident timeline
  5. sends an "incident changed" message to **SQS**
- Backend long-polls SQS and pushes over the WebSocket. Alarm returning to `OK` is recorded on the timeline.
- A least-privilege IAM role for the Lambda (only this table, this bucket, this queue, and reading these logs).
- **🎯 Milestone: inject a failure on the dashboard → about 2 min later a real incident pops up live, with evidence in S3.** From here on there is always a demo.

### Step 7: Smart incident logic (deterministic, no AI)
- **Dependency graph** in config (`services.yaml`), served at `/services/{id}/dependencies` and drawn on the dashboard.
- **Correlation:** alarms within a time window on services connected in the graph attach to one incident as "alerts" instead of becoming new incidents.
- **Root-service analysis:** among correlated services, the one that failed *first* and that the others depend on is the "probable root"; the rest are "downstream impact". Shown as "PROBABLE CASCADING FAILURE".
- **Severity rules** (SEV-1 to SEV-4) from availability, error rate and latency vs. baseline, recalculated as alerts arrive.
- Unit tests for all of this, since it's the core algorithm the product depends on.
- **Done when:** `db_slow` produces exactly **1 incident** with payment as the root and order as downstream, not 2+ incidents.

### Step 8: AI diagnosis + human approval (Bedrock)
- `backend/diagnosis/` (the only module that touches the LLM): builds a structured prompt from facts (metrics, log excerpts from S3, timeline, dependency analysis) and calls Bedrock Converse with a **JSON schema** for the output (`summary, likely_root_cause, confidence, evidence[], recommended_actions[]`). The model ID comes from config, and it must be a model available in-Region in us-east-2.
- Runs in the background when an incident is created. If Bedrock fails, the incident still works and shows "AI analysis unavailable".
- UI clearly separates **OBSERVED FACTS** from **AI INFERENCE**.
- Recommended actions come only from a fixed **allow-list** (`restart_service`, `clear_chaos`, `scale_pool`, etc.). The engineer clicks **Approve** or **Reject**, and only Approve triggers the simulated remediation. Every decision is logged on the timeline. The model never gets shell or cloud access.
- **Done when:** the diagnosis names connection-pool exhaustion with evidence; approving it recovers the service; the alarms return to OK; you resolve the incident.

### Step 9: Incident detail page + evaluation harness
- Detail page: metric graphs (latency, errors, requests from CloudWatch), searchable logs, dependency view, related alerts, AI panel, full timeline, controls (ack/assign/severity/resolve).
- More failure types: `crash` (dependency down), `intermittent`, `cpu`.
- `scenarios/run.py`: injects N labeled failures one after another and records **time to detect**, **alarms per incident**, **root service correct?** and **AI root cause correct?** into a results table.
- **Done when:** you have real measured performance numbers, e.g. "identified the root service in 18/20 cascading failures; median detection 74 s".

### Step 10: Infrastructure as code, hardening, presentation
- **AWS CDK (Python)** in `infra/` defines DynamoDB, S3, SQS, Lambda, EventBridge, alarms, IAM roles and EC2. Test it by tearing everything down and running `cdk deploy` from scratch.
- Security pass: review IAM policies, block S3 public access, a simple login for the dashboard, secrets out of the code.
- README with architecture diagram, design decisions (why SQS, why conditional writes, why deterministic severity), cost notes and evaluation results. **2–3 minute demo video** for users and early adopters.
- Decide on upgrading the AWS account to the Paid Plan (+ spend limit) before real users.
- **Stage 1 complete.** Stage 2 (RAG, LangGraph, MCP, agents) only starts after this.

---

## Team workflow
- **Claude** writes the code for each step on a feature branch and opens the PR(s).
- **A second AI reviewer (OpenAI)** reviews each PR and merges it if it's good.
- **Ghadi** directs the work, does anything involving AWS accounts or billing, and has final say.
- `main` is protected: a PR can only merge when CI passes. Use **squash merge** and delete the branch afterwards.

## Pull requests while working alone: **yes, lightweight ones**
- **Workflow:** `git switch -c step-3-backend` → small commits → `gh pr create` → CI runs tests → read your own diff in the GitHub UI (Claude can also run a code review on it) → **squash-merge** → delete the branch. No required approvals.
- **Why:**
  1. CI catches breakage before it reaches `main`, so `main` always works and there is always a demo.
  2. A clean history: every change has a description of *what and why*, which matters once contributors, users or investors look at the repo.
  3. PR descriptions become a design log ("PR #6 fixed a race where concurrent alarms created duplicate incidents…").
  4. It's how professional teams ship (code review on every change), and it scales when you add teammates.
- Skip PRs only for trivial fixes like README typos.

## Cost expectations (paid from the $100 Free Plan credits)
EC2 about $0.04/hr only while running (~$30/month if left on 24/7, so stop it when not working). CloudWatch custom metrics and alarms around $5–10/month. Lambda, SQS, DynamoDB and S3 about $0 at our scale. Bedrock is fractions of a cent per diagnosis. The **$20 budget alert** emails you as credits are used.

## Verification (final end-to-end demo)
1. `cdk deploy` + `docker compose up -d` on EC2 → dashboard all green.
2. Click **Inject: Payment DB slow** → payment logs show pool exhaustion → about 2 min later the alarm fires → Lambda → **one** incident appears live (no refresh), payment red, order yellow.
3. Incident shows correlated alerts, "root: payment, downstream: order", deterministic severity, evidence files in S3.
4. AI panel shows the diagnosis with confidence and evidence, separate from facts → **Approve** "clear db_slow / restart payment" → metrics recover → alarms return to OK → **Resolve** → full timeline remains.
5. `pytest` (correlation, severity, Lambda handler with mocked AWS via `moto`) and CI are green; `scenarios/run.py` produces the evaluation table.
