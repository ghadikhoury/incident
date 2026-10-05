# Deploying to EC2

Incident runs on one EC2 instance: the same `docker compose` stack as on a laptop, started by a
first-boot script. The original demo was created with the AWS CLI. `infra/` now
defines its replacement in AWS CDK; see the migration procedure below before
running `cdk deploy` against an account with existing demo resources.

```
your laptop ══ SSH (port 22, your key) ══▶ EC2 t3.small (Ubuntu 24.04, Docker)
  browser → localhost:3001 ─┘ tunnel        ├─ frontend (nginx, 127.0.0.1:3000) ─▶ backend ─▶ DynamoDB incident-store
                                            └─ gateway, order, payment, inventory, postgres, loadgen
                                                 │ every container's logs (awslogs driver)
                                                 ▼
                                   CloudWatch Logs /incident/<service> ─▶ metrics (EMF) ─▶ alarms
security group: only port 22 (SSH), only from your IP. No other port is open.
IAM role:       scoped table, queue, evidence, log, metric and Bedrock access (details below)
```

## What each piece is for

| Piece | Why |
|---|---|
| `t3.small` | Free-Plan accounts may only launch Free Tier-eligible types (t3.micro/small, t4g.micro/small, c7i-flex.large, m7i-flex.large). 2 GB RAM fits the stack (~0.5 GB) plus 2 GB swap for builds. |
| Security group | A firewall around the instance. Only SSH is open, and only from your IP. |
| SSH tunnel to the dashboard | The dashboard binds to `127.0.0.1` and is reached through SSH. The EC2 nginx layer also requires a generated login for the UI, API, and WebSocket. The `/ready` endpoint returns only a readiness status and is exempt from login. |
| IAM role + instance profile | Short-lived credentials through instance metadata, so no access keys are stored on the server. The backend can use the incident table, update queue, saved evidence, metrics, log queries and one Bedrock model; Docker can write to `/incident/*` log groups. It cannot create tables, buckets, queues or alarms. CDK defines the role for fresh deployments; `deploy/iam/instance-policy.json` is for the original CLI deployment. |
| Metadata hop limit 2 | IMDSv2 tokens are needed (`HttpTokens=required`). A container is one network hop further away than the host, so with the default hop limit of 1 the backend container couldn't get credentials. |
| `deploy/user-data.sh` | Runs once at first boot: swap, Docker, `git clone`, `.env`, `docker compose up`, then `verify.sh`. |
| `deploy/incident.service` | Starts the stack again after the instance is stopped and started. |
| `deploy/update.sh` | Deploys new code: `git pull`, rebuild what changed, then `verify.sh`. |
| `deploy/verify.sh` | Checks the deployment end to end through nginx, including a real DynamoDB read. The containers' health checks alone would pass even if the table were missing. |
| `docker-compose.ec2.yml` | EC2-only layer (selected by `COMPOSE_FILE` in the instance's `.env`): sends each container's logs to CloudWatch Logs with Docker's `awslogs` driver. |
| `deploy/setup-cloudwatch.sh` | Creates the log groups (14-day retention) and the alarms. Safe to rerun; edit a threshold and rerun to change it. |

## One-time setup

### CDK deployment on a fresh account/region

The default CDK stack uses the same fixed resource names as the original CLI
demo. CloudFormation cannot create a second `incident-store`, evidence bucket,
alarm set, or Lambda alongside those resources. **Do not deploy the default
stack in the current us-east-2 account until the existing data has been
exported and the migration is approved.** `cdk synth` and the CI template
tests are safe and do not change AWS resources. The default stack's table,
evidence bucket, log groups, and queues have `Retain` removal policies to
prevent accidental incident-history loss.

An isolated verification stack can coexist with the CLI demo. Pass
`-c stage=verify -c branch=step-10-final` (or a different Git branch containing
the stage support) to every CDK command. It names its own table, bucket,
queues, logs, alarms, Lambda, roles, VPC, and EC2 instance; it also uses a
separate metric namespace. It does not retain its data resources on deletion.
Only destroy a stage after checking the account and exact stack name. The
evidence bucket must be emptied before `cdk destroy` if a scenario wrote
evidence. In-flight backfill may write more evidence during deletion. If S3
reports `DELETE_FAILED` because the bucket is not empty, wait for the instance
and processor to finish deleting, empty that same stage bucket again, and retry
`cdk destroy`. Its data is disposable; the original demo data is unaffected.

For an empty account/region, using the `incident` AWS profile and an existing
EC2 key pair, from the repository root:

```bash
python -m pip install -r requirements-dev.txt -r infra/requirements.txt
python -m infra.build
ACCOUNT_ID=$(aws sts get-caller-identity --profile incident --query Account --output text)
MY_IP=$(curl -s https://checkip.amazonaws.com)
npx aws-cdk bootstrap aws://$ACCOUNT_ID/us-east-2 --profile incident
npx aws-cdk synth -c sshCidr=$MY_IP/32 -c keyName=incident-ec2 --profile incident
npx aws-cdk deploy -c sshCidr=$MY_IP/32 -c keyName=incident-ec2 --profile incident
```

Use the stack outputs for the instance ID, public IP, SQS update URL, and
evidence bucket. First boot writes the queue URL and bucket to `.env`, creates
the dashboard login, and starts Compose. After the stack is deployed, connect
by SSH and run `sudo /opt/incident/deploy/verify.sh`. CDK's bootstrap staging
bucket is separate from the private evidence bucket. The CDK stack includes a
new public VPC subnet without a NAT gateway; the security group admits only
SSH from `sshCidr`.

For the **existing** CLI deployment, keep using the commands below until a
reviewed migration plan exports the table and evidence, verifies their hashes,
retires the old named resources, deploys CDK, and restores the data. The CDK
template has been tested locally; a destructive fresh deployment in the
current account is pending authorization.

When upgrading an instance from a revision older than dashboard login, first
fetch and check out the new branch, run
`sudo /opt/incident/deploy/setup-dashboard-auth.sh`, then run
`sudo /opt/incident/deploy/update.sh <branch>`. A running old `update.sh`
cannot execute lines added to the new revision after its checkout; following
this order creates the bind-mounted credential file before Compose starts.
If an old updater has already created `/etc/incident/dashboard.htpasswd` as
an empty directory, run the new updater again; it removes that directory and
rebinds the frontend to the generated file.

**Account decision:** keep the AWS Free Plan for this demo. Before inviting
real users, the account owner should confirm the Paid Plan upgrade, set a
monthly spend limit and billing alert, and obtain enough Bedrock quota to
exercise the AI diagnosis. Neither an account upgrade nor a billing change is
performed by this repository or by `cdk deploy`.

### Original CLI deployment

Run from the repo root in Git Bash (or any bash), logged in with `aws login --profile incident`.

```bash
# MSYS_NO_PATHCONV=1 stops Git Bash on Windows from rewriting arguments such as /dev/sda1
# and /aws/service/... into Windows paths (harmless elsewhere).
export AWS_PROFILE=incident AWS_REGION=us-east-2 MSYS_NO_PATHCONV=1
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
MY_IP=$(curl -s https://checkip.amazonaws.com)

# 0. The DynamoDB table and its indexes, then CloudWatch log groups and alarms.
#    Both are safe to rerun, and both use your local profile: the instance's role is
#    deliberately not allowed to create these.
(cd backend && INCIDENT_AWS_PROFILE=incident python -m incident_api.setup_table)
deploy/setup-cloudwatch.sh

# 1. IAM role the instance runs as
aws iam create-role --role-name incident-ec2 \
  --assume-role-policy-document file://deploy/iam/ec2-trust-policy.json
sed "s/ACCOUNT_ID/$ACCOUNT_ID/" deploy/iam/instance-policy.json > instance-policy.tmp.json
aws iam put-role-policy --role-name incident-ec2 --policy-name incident-instance \
  --policy-document file://instance-policy.tmp.json && rm instance-policy.tmp.json
aws iam create-instance-profile --instance-profile-name incident-ec2
aws iam add-role-to-instance-profile --instance-profile-name incident-ec2 --role-name incident-ec2

# 2. Firewall: SSH from your IP only. Nothing else is opened.
SG_ID=$(aws ec2 create-security-group --group-name incident-ec2 \
  --description "Incident: SSH from one IP" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --ip-permissions \
  "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$MY_IP/32,Description=ssh}]"

# 3. SSH key pair (the private key stays on your machine, never in git)
mkdir -p ~/.ssh
# (tr: on Windows the CLI prints \r\n line endings, which OpenSSH can't read)
aws ec2 create-key-pair --key-name incident-ec2 --key-type ed25519 \
  --query KeyMaterial --output text | tr -d '\r' > ~/.ssh/incident-ec2.pem
chmod 600 ~/.ssh/incident-ec2.pem  # Windows: icacls %USERPROFILE%\.ssh\incident-ec2.pem /inheritance:r /grant:r "%USERNAME%:R"

# 4. Launch (latest Ubuntu 24.04 image, 20 GB encrypted disk)
AMI_ID=$(aws ssm get-parameter --query Parameter.Value --output text \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id)
INSTANCE_ID=$(aws ec2 run-instances --image-id "$AMI_ID" --instance-type t3.small \
  --key-name incident-ec2 --security-group-ids "$SG_ID" \
  --iam-instance-profile Name=incident-ec2 \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}' \
  --user-data file://deploy/user-data.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=incident},{Key=Project,Value=incident}]' \
  --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

# 5. After 3-5 minutes, check that first boot succeeded. The last line should start with "OK:".
ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP 'cloud-init status --wait; sudo tail -3 /var/log/cloud-init-output.log'
```

If `run-instances` fails with "Invalid IAM Instance Profile", wait ~10 seconds and run it
again: a new instance profile takes a moment to become visible to EC2.

## Opening the dashboard

```bash
ssh -i ~/.ssh/incident-ec2.pem -N -L 3001:127.0.0.1:3000 ubuntu@$IP
```

Leave that running and open **http://localhost:3001**. Get the login on the instance
with `sudo cat /etc/incident/dashboard-credentials` (username `incident`; the
password is generated at first boot and retained across updates). Never paste it
in a PR or log. `-L 3001:127.0.0.1:3000` forwards your
laptop's port 3001, through the encrypted SSH connection, to port 3000 on the instance itself.
(3001 rather than 3000 so it doesn't clash with a local `docker compose` stack.) `-N` means
"just forward, don't open a shell". Stop the tunnel with Ctrl+C.

## Everyday operations

```bash
export AWS_PROFILE=incident AWS_REGION=us-east-2 MSYS_NO_PATHCONV=1
INSTANCE_ID=$(aws ec2 describe-instances --filters Name=tag:Name,Values=incident \
  Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP                                 # log in
ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP sudo /opt/incident/deploy/update.sh   # deploy main
ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP sudo /opt/incident/deploy/verify.sh   # health check
ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP 'cd /opt/incident && docker compose ps'
ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP sudo tail -50 /var/log/cloud-init-output.log  # first-boot log

aws ec2 stop-instances --instance-ids "$INSTANCE_ID"    # stop compute charges
aws ec2 start-instances --instance-ids "$INSTANCE_ID"   # the stack starts by itself
```

**What it costs, and why to stop it.** Everything is paid from the Free Plan credits:
- **While running:** the instance (about $0.02/hour for t3.small), its public IPv4 address (about $0.005/hour), and the disk.
- **While stopped:** the instance and the public IP stop costing anything, but the **20 GB disk keeps using credits** (about $1.60/month for gp3) until the instance is terminated.

So stop the instance whenever you're not using it, and terminate it (see below) if you're done
with it. The public IP changes after every stop/start, so look it up again with the commands above.

**When your own IP changes** (new network, campus Wi-Fi), SSH is blocked. Replace the rule:

```bash
SG_ID=$(aws ec2 describe-security-groups --group-names incident-ec2 --query 'SecurityGroups[0].GroupId' --output text)
OLD=$(aws ec2 describe-security-groups --group-ids "$SG_ID" --query 'SecurityGroups[0].IpPermissions' --output json)
aws ec2 revoke-security-group-ingress --group-id "$SG_ID" --ip-permissions "$OLD"
MY_IP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --ip-permissions \
  "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$MY_IP/32,Description=ssh}]"
```

## Monitoring (CloudWatch)

Every container's log lines go to the CloudWatch log group `/incident/<service>`. The services'
JSON request lines and the backend's health checks contain [Embedded Metric Format](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html)
blocks, which CloudWatch turns into metrics in the `Incident` namespace, per `Service`:

| Metric | Meaning |
|---|---|
| `Latency` | Request latency in ms (one data point per request) |
| `Requests` | 1 per request |
| `Errors` | 1 per 5xx response |
| `HealthCheckFailed` | 1 if a health check (every 3 s, from the backend) found the service unhealthy or down, else 0 |

`deploy/setup-cloudwatch.sh` creates three alarms per service (gateway, order, payment,
inventory), each over 1-minute periods, firing when **2 of the last 3** minutes breach:

| Alarm | Fires when |
|---|---|
| `incident-<service>-latency` | p90 latency above 2000 ms |
| `incident-<service>-errors` | more than 20% of requests return 5xx |
| `incident-<service>-health` | more than half of the health checks failed |

Request metrics can be absent when there is no traffic, so the latency and error alarms treat
missing data as OK. The backend emits health checks every 3 seconds while it is running; the
health alarms instead show `INSUFFICIENT_DATA` when those samples stop arriving. That state
means monitoring is unavailable, **not** that the service recovered. Check the EC2 instance
state first: `stopped` is expected when you intentionally pause the demo; if it is `running`,
check `incident.service`, the backend container, and CloudWatch log delivery. The Step 6
pipeline records a monitoring-degraded incident while EC2 is running and does not interpret
`INSUFFICIENT_DATA` as an `OK` recovery event.

Where to look: CloudWatch console, Logs → Log groups → `/incident/payment`, Metrics → All
metrics → `Incident`, and Alarms. From the CLI:

```bash
aws cloudwatch describe-alarms --alarm-name-prefix incident- \
  --query 'MetricAlarms[].[AlarmName,StateValue]' --output table
aws logs tail /incident/payment --since 5m --follow
```

Injecting `db_slow` on payment (dashboard → Simulation) flips payment's alarms to `ALARM`
within about 2-3 minutes, followed by order's (the cascade). Recovering returns them to `OK`
a few minutes later.

## Step 6: automatic incident pipeline

From the repository root, after the table, CloudWatch log groups and alarms exist:

```bash
AWS_PROFILE=incident python deploy/setup_pipeline.py --instance-id i-08d5fddbdc352cc57
```

The script packages Linux Lambda dependencies, creates the private evidence bucket
`incident-evidence-<account>` (30-day evidence retention), SQS queue `incident-updates`,
an evidence-backfill queue and a 14-day failure queue, Lambda role and function, and
the EventBridge alarm-state rule. EventBridge delivery failures, exhausted Lambda
asynchronous retries, and failed backfill messages go to the failure queue for inspection.
It updates the EC2 role to receive queue messages. Rerunning it updates the Lambda code
and configuration.
Use the instance ID for the current demo instance if it changes.

Set `INCIDENT_QUEUE_URL` to the printed SQS URL in the instance's `/opt/incident/.env`,
then deploy the Step 6 branch with `sudo /opt/incident/deploy/update.sh <branch>` or
`main` after merge. A local Compose deployment can set the same variable in its `.env`.
The backend uses long polling and broadcasts each updated incident over `/api/ws`.
The Lambda creates one incident for alarms processed within a sliding ten-minute window;
Step 7 replaces that coarse grouping with dependency-aware correlation. EventBridge
delivery can be repeated, so event IDs are stored in DynamoDB and retries reuse the
same incident and evidence timeline entry. Resolving an incident lets the next alarm
start a new incident. Returning to `OK` adds a timeline entry but leaves resolution to
the engineer. A health alarm entering `INSUFFICIENT_DATA` while EC2 is running creates
or updates a monitoring-degraded incident; while EC2 is stopped it is ignored.

To verify, inject `db_slow` on the dashboard, wait for the payment CloudWatch alarm to
enter `ALARM`, and check that a new incident appears without refreshing the page. Open
its timeline via `/api/incidents/<id>` and find the evidence path. The bucket contains
`event.json`, `logs.json`, and `metrics.json` under
`incidents/<id>/events/<eventbridge-id>/`. The first snapshot is captured immediately
and marked `complete: false` because the five minutes after the alarm have not yet
elapsed. A delayed backfill replaces `logs.json` and `metrics.json` with the full
five minutes before and after the alarm and marks them `complete: true`. The log
evidence has per-minute counts across the full window and up to 200 early warning/error
samples; it excludes the simulation control endpoint and injection messages so they
do not reveal the diagnosis. The incident is broadcast as soon as its record exists,
even if evidence capture later fails; evidence and backfill each trigger another update.

The Lambda's log group is `/aws/lambda/incident-processor` (14-day retention). If an
alarm transitions but no incident appears, inspect that group, the EventBridge target,
and `incident-pipeline-failures`. The update queue retains messages for four days and
redelivers when a backend broadcast fails; the initial incident API fetch covers updates
missed while a browser was disconnected. Inspect the failure queue before replaying a
failed alarm or backfill: alarm events are idempotent in DynamoDB, and backfills replace
the same S3 objects. Logs Insights scans and S3 storage add usage-based charges.

## Step 7: dependency-aware incidents

`backend/incident_api/services.yaml` defines the service graph and reference p90
latencies. The API serves the graph at `/api/services/graph` and direct, transitive,
and reverse dependencies at `/api/services/<name>/dependencies`. The dashboard draws
the graph and shows each automatic incident's alerts, probable root, downstream
services, and severity reason. **PROBABLE CASCADING FAILURE** is an inference from
observed alarms, not a confirmed diagnosis.

The Lambda correlates alarm transitions within a sliding ten-minute event-time
window. One service must depend on the other, directly or transitively. Sibling
services such as payment and inventory stay in separate incidents. A conditional
DynamoDB write serializes simultaneous alarm events, and each incident stores one
current alert per CloudWatch alarm. The probable root is the alerted service on
which the most other alerted services depend; the first breaching metric period
(or the alarm transition time when metric timestamps are absent) breaks ties.
Resolved incidents are never reused.

Severity is recalculated as alarms fire or recover: SEV-1 for a gateway outage or
very high gateway error rate, failed health checks on two services, or major impact
across three services; SEV-2 for one failed health check, at least 50% 5xx errors,
or p90 latency at least twice the alarm threshold and ten times its configured
baseline; SEV-3 for other error or latency alarms; SEV-4 for missing monitoring
data or when all alerts recover. The rules use values in CloudWatch alarm events,
not simulation settings or an AI model.

To deploy before merge, run `AWS_PROFILE=incident python deploy/setup_pipeline.py
--instance-id <instance-id>` from the branch, then run `sudo
/opt/incident/deploy/update.sh <branch>` on EC2. After merge, use `main`. The
Lambda package includes the graph config and PyYAML; no table migration is needed.
For an end-to-end check, inject `db_slow` on payment and wait for the cascade:
one incident should show payment as probable root, order as downstream, and
multiple alerts. Recover payment and resolve the incident when finished.

## Gemini backend key for an existing deployment

The provider and dashboard changes are merged. Deploy the updated main branch
when updating an existing EC2 installation.
An existing EC2 `.env` without `DIAGNOSIS_PROVIDER` remains on Bedrock because
Compose explicitly selects it for legacy configurations. Fresh EC2 user data
also explicitly selects Bedrock. The application default and local
`.env.example` select Gemini, but neither changes those EC2 settings. Bedrock's
current zero quota can still produce `UNAVAILABLE`; the upgrade does not switch
those installations to an unconfigured Gemini provider.

After the code is deployed, provide a new key privately to the operator. On the
instance, edit `/opt/incident/.env` privately and set `GEMINI_API_KEY`,
`GEMINI_MODEL=gemini-3.5-flash-lite`, and `DIAGNOSIS_PROVIDER=gemini` together.
Keep the file owner-only (`sudo chmod 600 /opt/incident/.env`). From
`/opt/incident`, restart only the backend with
`sudo docker compose up -d --no-deps --force-recreate --wait backend`, then
verify a synthetic incident diagnosis. An explicit Gemini selection without a
key yields `UNAVAILABLE`; it never falls back to Bedrock.
Do not put a key in EC2 user data, shell history, the repository, or a dashboard
request. The existing `deploy/update.sh` preserves `.env` on the instance.
Use only synthetic incident evidence with the Gemini free tier. Check the
selected model and the API project's quota before a live call. The older
alias returned HTTP 503 during the 2026-10-04 smoke check; the repository now
defaults to `gemini-3.5-flash-lite`, which succeeded in the
[real local READY walkthrough](LOCAL_DEMO_VERIFICATION.md). In the
2026-10-04 `INC-1020` scenario, the existing EC2 pipeline saved seven
synthetic evidence events. A local Gemini worker retried the prior
`UNAVAILABLE` analysis and wrote `READY`; the EC2 dashboard showed three
supporting evidence lines and a `PENDING` payment reset suggestion. The fault
was restored via simulation controls. The EC2 backend itself still uses its
previous release, so deploy the provider code and privately configure the key
before claiming end-to-end deployed Gemini operation. For Bedrock later, set `DIAGNOSIS_PROVIDER=bedrock` and keep the existing
`BEDROCK_MODEL_ID` and IAM policy. A provider failure remains `UNAVAILABLE`;
there is no automatic fallback.

## Historical Step 8: Bedrock diagnosis and approved remediation

Run `AWS_PROFILE=incident python deploy/setup_diagnosis.py` from the Step 8 branch
before deploying it to EC2 with `sudo /opt/incident/deploy/update.sh <branch>`.
The setup script updates only the backend instance role: it can read saved incident
evidence and invoke `openai.gpt-oss-20b-1:0` in us-east-2. The evidence bucket name
defaults to `incident-evidence-<AWS account ID>`; set `INCIDENT_EVIDENCE_BUCKET` in
`.env` only if the bucket differs. `BEDROCK_MODEL_ID` is configurable, but a different
model also needs its ARN added to `deploy/iam/instance-policy.json` and
`deploy/setup_diagnosis.py` rerun.

After the first alarm, the backend waits 100 seconds for the cascade's later alarms
and evidence before diagnosing it. It reads
bounded S3 log and metric excerpts, sends structured facts to Bedrock Converse, and
stores the JSON result with the incident. If Bedrock is unavailable, the incident
and its observed alerts stay visible with **AI analysis unavailable**. An engineer
can use **Retry analysis** after the model becomes available; that request is logged
and starts immediately. A stale worker claim is retried after five minutes. The
dashboard distinguishes **Observed facts**
from **AI inference**. Model output can only suggest `clear_chaos` on the root
monitored service; it cannot run commands or call AWS. An engineer must enter a name
and click **Approve and run** or **Reject**. The decision and outcome are recorded
on the timeline. Rejected suggestions never reach a service; approved resets are
idempotent and a stranded approved/executing reset is retried by the backend.

For a live check, inject `db_slow` on payment, wait for a CloudWatch incident and
its diagnosis, inspect the cited pool/timeout evidence, then approve the suggested
payment reset. Verify payment's chaos state returns to zero and the related alarms
return to OK before resolving the incident. Bedrock is a paid, quota-controlled
service. On 2026-10-03 this AWS account reported **zero on-demand requests/minute**
for the in-region structured-output models checked, so Bedrock calls returned
`ThrottlingException` before a diagnosis could be produced. A positive applied
Bedrock model quota is required for this live check; until then the unavailable
state is expected.

## Step 9: incident detail and measured scenarios

From the Step 9 branch, run `AWS_PROFILE=incident python deploy/setup_diagnosis.py`
on the operator's computer before deploying the branch. This reapplies the EC2 role
policy with read-only `cloudwatch:GetMetricData` and bounded CloudWatch Logs Insights
query permissions. Then run `sudo /opt/incident/deploy/update.sh <branch>` on EC2.

Open an incident's **Review** page. Its charts request one-minute CloudWatch latency,
request, and error data for the incident's correlated services. The initial log list
comes from saved S3 error/warning excerpts; entering a term and clicking **Search
CloudWatch** searches all matching log lines in the incident's 30-minute window.
The search is limited to 200 lines, excludes injection-control lines, and runs only
when requested. The page also shows related alerts, the dependency graph, AI analysis,
the full timeline, and acknowledge, owner, severity, and resolve controls.

The demo now supports `intermittent` (all requests fail for two minutes, then recover
for one minute, repeating) and `cpu` (2.3 seconds of bounded CPU work per request),
as well as the earlier `crash` scenario. The CPU work runs in a worker thread; Python's
GIL still competes with the event loop, so latency can rise. There is no CPU alarm;
the existing latency alarm detects this case. Each service
also emits measured process CPU utilization every 10 seconds, so a CPU diagnosis can
cite a real signal rather than injection settings. A crashed
service cannot answer `/chaos`; restart its container with `sudo docker compose start
<service>` from `/opt/incident`, then clear any remaining chaos settings if needed.
Clearing the CPU setting also stops work already queued in worker threads.

The evaluation runner uses only the backend's localhost API and standard Python. Run it
on EC2 from `/opt/incident` when all services are healthy and no incident is active:

```bash
python3 scenarios/run.py --cases db_slow,intermittent,cpu \
  --output scenarios/results/step9.json
# Include crash only when you intentionally allow the runner to restart its container:
sudo python3 scenarios/run.py --cases crash --allow-crash-restart \
  --output scenarios/results/crash.json
```

Each case injects one labeled fault, polls for the first new CloudWatch incident,
waits for Step 8 analysis, restores the fault, waits for every attached alarm and
service to recover, and resolves the incident. It stops after an incomplete case.
The JSON and CSV outputs record detection seconds, alarm count, exact root-service
correctness, AI status, and an explicit *keyword-pattern estimate* of AI cause
correctness. A quota failure is `UNAVAILABLE` and has no AI correctness score. Review
the diagnosis text manually before treating that estimate as an accuracy claim.
The polling interval adds up to five seconds to measured detection latency.
The default recovery wait is eight minutes: heavy CPU work can leave queued requests
with high latency for several minutes after the setting is cleared, and CloudWatch
needs more healthy periods before its alarms return to OK.

**Cost and the $20 usage budget.** At list rates, 20 custom metrics are about $6/month.
The eight latency/health alarms each evaluate one metric, while the four error-rate alarms
each evaluate two: **16 alarm-metric units**, about $1.60/month before any free allowance.
CloudWatch Logs also bills for ingestion. A 15-minute sample of this project's eight live
log groups at the default 5 requests/second ingested 10.9 MB. If that rate ran all month,
it would be about 31 GB of logs, not a few cents: using AWS's US-East example of 5 GB
free and $0.50/GB thereafter, ingestion alone would be about $13/month, before log storage,
EC2, its disk and public IPv4 address. This is an extrapolation, not a bill; rates and
allowances vary by Region and account. **Do not leave the demo running continuously:** that
would exceed the $20 usage budget. Stop the instance when you finish a session, and check
Billing/Cost Explorer as usage accumulates. See [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/).

**Upgrading an instance created before CloudWatch existed:** after this PR is merged to
`main`, run the following from the repo root in Git Bash. Set up the log groups and alarms,
then grant the existing instance role log-write access **before** enabling the EC2 compose
layer. The old inline policy is removed after the replacement is installed.

```bash
export AWS_PROFILE=incident AWS_REGION=us-east-2 MSYS_NO_PATHCONV=1
deploy/setup-cloudwatch.sh
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
sed "s/ACCOUNT_ID/$ACCOUNT_ID/" deploy/iam/instance-policy.json > instance-policy.tmp.json
aws iam put-role-policy --role-name incident-ec2 --policy-name incident-instance \
  --policy-document file://instance-policy.tmp.json && rm instance-policy.tmp.json
if aws iam get-role-policy --role-name incident-ec2 --policy-name incident-backend >/dev/null 2>&1; then
  aws iam delete-role-policy --role-name incident-ec2 --policy-name incident-backend
fi

INSTANCE_ID=$(aws ec2 describe-instances --filters Name=tag:Name,Values=incident \
  Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP \
  "sudo sed -i '/^COMPOSE_FILE=/d' /opt/incident/.env && \
   printf 'COMPOSE_FILE=docker-compose.yml:docker-compose.ec2.yml\n' | sudo tee -a /opt/incident/.env >/dev/null"
ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP sudo /opt/incident/deploy/update.sh main
```

The last command ends with `verify.sh`. After it reports `OK`, check that a new request log
appears in `/incident/payment` and the alarms are present with the CLI commands above. IAM
policy changes can take a short time to reach EC2; if Docker reports an access-denied error
when creating log streams, wait a minute and rerun `update.sh`.

## Tearing it all down

```bash
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" && aws ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"
aws ec2 delete-security-group --group-name incident-ec2
aws ec2 delete-key-pair --key-name incident-ec2
aws iam remove-role-from-instance-profile --instance-profile-name incident-ec2 --role-name incident-ec2
aws iam delete-instance-profile --instance-profile-name incident-ec2
aws iam delete-role-policy --role-name incident-ec2 --policy-name incident-instance
aws iam delete-role --role-name incident-ec2
aws cloudwatch delete-alarms --alarm-names $(aws cloudwatch describe-alarms --alarm-name-prefix incident- --query 'MetricAlarms[].AlarmName' --output text)
for g in gateway order payment inventory loadgen backend frontend postgres; do aws logs delete-log-group --log-group-name "/incident/$g"; done
```

The DynamoDB table is separate and is not deleted by this.
Step 6 also creates an EventBridge rule, Lambda function and role, SQS queue, and S3 bucket;
remove those separately if retiring the demo. The S3 bucket contains incident evidence,
so inspect it before deleting it.
