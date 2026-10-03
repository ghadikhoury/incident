# Deploying to EC2

Incident runs on one EC2 instance: the same `docker compose` stack as on a laptop, started by a
first-boot script. Infrastructure is created with the AWS CLI for now; Step 10 replaces these
commands with AWS CDK.

```
your laptop ══ SSH (port 22, your key) ══▶ EC2 t3.small (Ubuntu 24.04, Docker)
  browser → localhost:3001 ─┘ tunnel        ├─ frontend (nginx, 127.0.0.1:3000) ─▶ backend ─▶ DynamoDB incident-store
                                            └─ gateway, order, payment, inventory, postgres, loadgen
security group: only port 22 (SSH), only from your IP. No other port is open.
IAM role:       the instance may read/write the incident-store table, nothing else
```

## What each piece is for

| Piece | Why |
|---|---|
| `t3.small` | Free-Plan accounts may only launch Free Tier-eligible types (t3.micro/small, t4g.micro/small, c7i-flex.large, m7i-flex.large). 2 GB RAM fits the stack (~0.5 GB) plus 2 GB swap for builds. |
| Security group | A firewall around the instance. Only SSH is open, and only from your IP. |
| SSH tunnel to the dashboard | The dashboard's API has no login yet (Step 10) and can create incidents and break services. A source-IP rule alone isn't enough to protect it: on a shared network (campus Wi-Fi, NAT) many people share one public IP. So the dashboard listens only on the instance's `127.0.0.1`, and you reach it through SSH, which requires your private key. |
| IAM role + instance profile | Gives the backend short-lived credentials for DynamoDB through the instance metadata service, so no access keys are stored on the server. Limited to reading and writing `incident-store` (see `deploy/iam/backend-policy.json`). It deliberately can't create or change tables, which is why the table is set up from your laptop (step 0). |
| Metadata hop limit 2 | IMDSv2 tokens are needed (`HttpTokens=required`). A container is one network hop further away than the host, so with the default hop limit of 1 the backend container couldn't get credentials. |
| `deploy/user-data.sh` | Runs once at first boot: swap, Docker, `git clone`, `.env`, `docker compose up`, then `verify.sh`. |
| `deploy/incident.service` | Starts the stack again after the instance is stopped and started. |
| `deploy/update.sh` | Deploys new code: `git pull`, rebuild what changed, then `verify.sh`. |
| `deploy/verify.sh` | Checks the deployment end to end through nginx, including a real DynamoDB read. The containers' health checks alone would pass even if the table were missing. |

## One-time setup

Run from the repo root in Git Bash (or any bash), logged in with `aws login --profile incident`.

```bash
# MSYS_NO_PATHCONV=1 stops Git Bash on Windows from rewriting arguments such as /dev/sda1
# and /aws/service/... into Windows paths (harmless elsewhere).
export AWS_PROFILE=incident AWS_REGION=us-east-2 MSYS_NO_PATHCONV=1
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
MY_IP=$(curl -s https://checkip.amazonaws.com)

# 0. The DynamoDB table and its indexes (safe to rerun; also upgrades an existing table).
#    Uses your local profile: the instance's role is deliberately not allowed to do this.
(cd backend && INCIDENT_AWS_PROFILE=incident python -m incident_api.setup_table)

# 1. IAM role the instance runs as
aws iam create-role --role-name incident-ec2 \
  --assume-role-policy-document file://deploy/iam/ec2-trust-policy.json
sed "s/ACCOUNT_ID/$ACCOUNT_ID/" deploy/iam/backend-policy.json > backend-policy.tmp.json
aws iam put-role-policy --role-name incident-ec2 --policy-name incident-backend \
  --policy-document file://backend-policy.tmp.json && rm backend-policy.tmp.json
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

## Tearing it all down

```bash
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" && aws ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"
aws ec2 delete-security-group --group-name incident-ec2
aws ec2 delete-key-pair --key-name incident-ec2
aws iam remove-role-from-instance-profile --instance-profile-name incident-ec2 --role-name incident-ec2
aws iam delete-instance-profile --instance-profile-name incident-ec2
aws iam delete-role-policy --role-name incident-ec2 --policy-name incident-backend
aws iam delete-role --role-name incident-ec2
```

The DynamoDB table is separate and is not deleted by this.
