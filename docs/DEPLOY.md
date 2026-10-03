# Deploying to EC2

Incident runs on one EC2 instance: the same `docker compose` stack as on a laptop, started by a
first-boot script. Infrastructure is created with the AWS CLI for now; Step 10 replaces these
commands with AWS CDK.

```
your laptop ══ SSH (port 22, your key) ══▶ EC2 t3.small (Ubuntu 24.04, Docker)
  browser → localhost:3001 ─┘ tunnel        ├─ frontend (nginx, 127.0.0.1:3000) ─▶ backend ─▶ DynamoDB incident-store
                                            └─ gateway, order, payment, inventory, postgres, loadgen
                                                 │ every container's logs (awslogs driver)
                                                 ▼
                                   CloudWatch Logs /incident/<service> ─▶ metrics (EMF) ─▶ alarms
security group: only port 22 (SSH), only from your IP. No other port is open.
IAM role:       read/write the incident-store table, write to the /incident/* log groups; nothing else
```

## What each piece is for

| Piece | Why |
|---|---|
| `t3.small` | Free-Plan accounts may only launch Free Tier-eligible types (t3.micro/small, t4g.micro/small, c7i-flex.large, m7i-flex.large). 2 GB RAM fits the stack (~0.5 GB) plus 2 GB swap for builds. |
| Security group | A firewall around the instance. Only SSH is open, and only from your IP. |
| SSH tunnel to the dashboard | The dashboard's API has no login yet (Step 10) and can create incidents and break services. A source-IP rule alone isn't enough to protect it: on a shared network (campus Wi-Fi, NAT) many people share one public IP. So the dashboard listens only on the instance's `127.0.0.1`, and you reach it through SSH, which requires your private key. |
| IAM role + instance profile | Short-lived credentials through the instance metadata service, so no access keys are stored on the server. Used by the backend (read/write `incident-store`) and by Docker's log driver (write to the `/incident/*` log groups). See `deploy/iam/instance-policy.json`. It deliberately can't create tables, log groups or alarms, which is why those are set up from your laptop (step 0). |
| Metadata hop limit 2 | IMDSv2 tokens are needed (`HttpTokens=required`). A container is one network hop further away than the host, so with the default hop limit of 1 the backend container couldn't get credentials. |
| `deploy/user-data.sh` | Runs once at first boot: swap, Docker, `git clone`, `.env`, `docker compose up`, then `verify.sh`. |
| `deploy/incident.service` | Starts the stack again after the instance is stopped and started. |
| `deploy/update.sh` | Deploys new code: `git pull`, rebuild what changed, then `verify.sh`. |
| `deploy/verify.sh` | Checks the deployment end to end through nginx, including a real DynamoDB read. The containers' health checks alone would pass even if the table were missing. |
| `docker-compose.ec2.yml` | EC2-only layer (selected by `COMPOSE_FILE` in the instance's `.env`): sends each container's logs to CloudWatch Logs with Docker's `awslogs` driver. |
| `deploy/setup-cloudwatch.sh` | Creates the log groups (14-day retention) and the alarms. Safe to rerun; edit a threshold and rerun to change it. |

## One-time setup

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

Leave that running and open **http://localhost:3001**. `-L 3001:127.0.0.1:3000` forwards your
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
ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP /opt/incident/deploy/verify.sh        # health check
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
Lambda role and function, and the EventBridge alarm-state rule. It updates the EC2 role
to receive queue messages. Rerunning it updates the Lambda code and configuration.
Use the instance ID for the current demo instance if it changes.

Set `INCIDENT_QUEUE_URL` to the printed SQS URL in the instance's `/opt/incident/.env`,
then deploy the Step 6 branch with `sudo /opt/incident/deploy/update.sh <branch>` or
`main` after merge. A local Compose deployment can set the same variable in its `.env`.
The backend uses long polling and broadcasts each updated incident over `/api/ws`.
The Lambda creates one incident for alarms processed within a ten-minute window;
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
`incidents/<id>/events/<eventbridge-id>/`. Evidence is a snapshot taken when Lambda
processes the alarm, so the future half of the requested five-minute window may not
have arrived yet. Logs Insights queries are capped at 200 results per event.

The Lambda's log group is `/aws/lambda/incident-processor` (14-day retention). If an
alarm transitions but no incident appears, inspect that group, the EventBridge target,
and the SQS queue. The queue retains messages for four days and redelivers when a
backend broadcast fails; the initial incident API fetch covers updates missed while a
browser was disconnected. Logs Insights scans and S3 storage add usage-based charges.

**Cost and the $20 usage budget.** At list rates, 16 custom metrics are about $4.80/month.
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
