# Local automatic incident detection

The standard Compose stack selects `INCIDENT_MODE=local`. The backend polls the
simulated services' `/health` endpoints and writes incidents, probe evidence, and
audit entries to SQLite in the `local_incidents` Docker volume. It does not read
or mutate DynamoDB, S3, SQS, or CloudWatch. Local Gemini diagnosis needs no AWS
credentials. Selecting Bedrock uses AWS only for the model call.

## Setup

Copy `.env.example` to the Git-ignored `.env` and select local mode. Keep a Gemini
key only in that private backend environment file; never put it in frontend
variables, screenshots, or source control. A model key is optional for detection.

```dotenv
INCIDENT_MODE=local
DIAGNOSIS_PROVIDER=gemini
GEMINI_MODEL=gemini-3.5-flash-lite
```

Set `GEMINI_API_KEY` privately if using Gemini. Use synthetic demo evidence only,
including on Google's free tier. Keep an account-supported free model selected;
there is no automatic provider or paid-model fallback.

```sh
docker compose up -d --build --wait
```

Open http://127.0.0.1:3000. `/api/environment` reports the mode, isolated record
source, detector availability, and model configuration without exposing secrets.
Direct `uvicorn` use defaults to AWS for compatibility; set `INCIDENT_MODE=local`
explicitly to use local detection and `LOCAL_DB_PATH` to choose its database.

## Observations and safeguards

- A failure must persist at least nine seconds and three consecutive probes.
  With the default three-second interval and two-second request timeout, expect
  roughly 9–17 seconds from failure to incident creation. Injection-triggered
  polls cannot bypass the elapsed-duration threshold. Unknown observations do
  not count as failures. A gap over 15 seconds resets the consecutive streak.
- Dependency correlation and severity use the same pure rules as AWS alarms.
  Related failures join an active failing episode; recently recovered related
  alerts can correlate within ten minutes. Unrelated sibling failures stay separate.
- Two healthy probes, at least three seconds apart, record `OK`. Recovery does
  **not** resolve the incident. Explicit engineer resolution is audited separately.
  A resolved failure must recover before it can create another incident.
- Probe state and incidents survive backend restarts. Run one backend instance
  for this local demo. The health polling loop is serialized with simulation polls.
- Evidence is actual `/health` status, error, latency, and timestamp. It excludes
  `/chaos` settings. Credential and personal-data redaction applies before saving
  probe errors and before model input. Retention preserves the first 100 and
  latest 700 samples per incident. This is probe evidence, **not container logs**;
  health latency is **not request latency**. Error-rate or latency injections may
  leave `/health` healthy and therefore do not automatically trigger this mode.
- Diagnosis waits six seconds after detection to collect more observations. It
  shares bounded prompts, untrusted-evidence instructions, Pydantic validation,
  provider selection, root-service action checks, and explicit human approval
  with the AWS path. Missing configuration or model failure yields `UNAVAILABLE`
  while the incident and evidence remain available. Retry is explicit and audited.
  A completed diagnosis is a timestamped snapshot; later recovery observations
  remain in the timeline and evidence rather than rewriting that inference.

## Crash and recovery walkthrough

Choose **inventory → Crash → Inject failure**. Active incidents initially stays
empty during the persistence window, then displays an automatically created
incident. Click **Review** for collected probes, graphs, related alerts, the
timeline, and AI status. No manual declaration is required.

A crashed container cannot answer the Recover endpoint. Incident displays an
operator command and does not expose Docker or shell execution through the API.
From this repository, check the stopped container and start it:

```sh
docker compose ps -a inventory
docker compose start inventory
```

Watch inventory become Healthy and its incident alert become `OK`. The incident
remains active until you enter an engineer name and explicitly Resolve it. An
approved `clear_chaos` action cannot restart a crashed container. For reachable
faults, Recover clears the fault but only reports success if `/health` verifies
recovery; local approved resets follow the same verification rule.

`docker compose down` retains local records. `docker compose down -v` deletes
**both** the local incident volume and simulation PostgreSQL data; use it only
when intentionally discarding the demo's data.

## AWS mode

The EC2 Compose override explicitly selects `INCIDENT_MODE=aws`. Its existing
CloudWatch → EventBridge → Lambda → DynamoDB/S3/SQS path remains in use. No local
detector runs in that mode. Outside the EC2 override, select AWS mode explicitly
and configure its table, evidence bucket, and queue as documented in DEPLOY.md.
Do not point local demo actions at an AWS-mode backend. No AWS infrastructure,
IAM, credentials, or deployed incidents need to change for local mode.

## Verification

Fake-based regression tests live in `backend/tests/test_local.py` and
`frontend/src/components/LocalDemo.test.tsx`. They cover persistence and elapsed
time, transient suppression, deduplication, dependency correlation, recovery,
restart durability, bounded/redacted evidence, local/AWS isolation, failed and
missing model configuration, explicit retry, and human approval. They need no
live key. The [verification report](LOCAL_DEMO_VERIFICATION.md) records the
completed real Gemini READY browser walkthrough and merged dashboard PR #17.
