# Deploying to EC2

Incident runs on one EC2 instance: the same `docker compose` stack as on a laptop, started by a
first-boot script. Infrastructure is created with the AWS CLI for now; Step 10 replaces these
commands with AWS CDK.

```
your browser ──:3000──▶ EC2 t3.small (Ubuntu 24.04, Docker)
                           ├─ frontend (nginx) ─▶ backend ─▶ DynamoDB incident-store
                           └─ gateway, order, payment, inventory, postgres, loadgen
security group: ports 22 (SSH) and 3000 (dashboard) open to ONE IP address only
IAM role:       the instance may read/write the incident-store table, nothing else
```

## What each piece is for

| Piece | Why |
|---|---|
| `t3.small` | Free-Plan accounts may only launch Free Tier-eligible types (t3.micro/small, t4g.micro/small, c7i-flex.large, m7i-flex.large). 2 GB RAM fits the stack (~0.5 GB) plus 2 GB swap for builds. About $0.02/hour from credits. |
| Security group | A firewall around the instance. SSH and the dashboard are reachable **only from your IP**: there's no login yet (Step 10), and the dashboard can break the simulated services. The service ports (8090-8093, 8000) aren't opened at all. |
| IAM role + instance profile | Gives the backend short-lived credentials for DynamoDB through the instance metadata service, so no access keys are stored on the server. Limited to `incident-store` (see `deploy/iam/backend-policy.json`). |
| Metadata hop limit 2 | IMDSv2 tokens are needed (`HttpTokens=required`). A container is one network hop further away than the host, so with the default hop limit of 1 the backend container couldn't get credentials. |
| `deploy/user-data.sh` | Runs once at first boot: swap, Docker, `git clone`, `.env`, `docker compose up`. |
| `deploy/incident.service` | Starts the stack again after the instance is stopped and started. |
| `deploy/update.sh` | Deploys new code: `git pull` + rebuild what changed. |

## One-time setup

Run from the repo root in Git Bash (or any bash), logged in with `aws login --profile incident`.

```bash
# MSYS_NO_PATHCONV=1 stops Git Bash on Windows from rewriting arguments such as /dev/sda1
# and /aws/service/... into Windows paths (harmless elsewhere).
export AWS_PROFILE=incident AWS_REGION=us-east-2 MSYS_NO_PATHCONV=1
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
MY_IP=$(curl -s https://checkip.amazonaws.com)

# 1. IAM role the instance runs as
aws iam create-role --role-name incident-ec2 \
  --assume-role-policy-document file://deploy/iam/ec2-trust-policy.json
sed "s/ACCOUNT_ID/$ACCOUNT_ID/" deploy/iam/backend-policy.json > backend-policy.tmp.json
aws iam put-role-policy --role-name incident-ec2 --policy-name incident-backend \
  --policy-document file://backend-policy.tmp.json && rm backend-policy.tmp.json
aws iam create-instance-profile --instance-profile-name incident-ec2
aws iam add-role-to-instance-profile --instance-profile-name incident-ec2 --role-name incident-ec2

# 2. Firewall: SSH and the dashboard from your IP only
SG_ID=$(aws ec2 create-security-group --group-name incident-ec2 \
  --description "Incident: SSH and dashboard from one IP" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --ip-permissions \
  "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$MY_IP/32,Description=ssh}]" \
  "IpProtocol=tcp,FromPort=3000,ToPort=3000,IpRanges=[{CidrIp=$MY_IP/32,Description=dashboard}]"

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
aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
```

First boot takes about 5 minutes (installing Docker and building the images). Then open
`http://<public-ip>:3000`.

If `run-instances` fails with "Invalid IAM Instance Profile", wait ~10 seconds and run it
again: a new instance profile takes a moment to become visible to EC2.

## Everyday operations

```bash
export AWS_PROFILE=incident AWS_REGION=us-east-2 MSYS_NO_PATHCONV=1
INSTANCE_ID=$(aws ec2 describe-instances --filters Name=tag:Name,Values=incident \
  Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP                       # log in
ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP sudo /opt/incident/deploy/update.sh   # deploy main
ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP 'cd /opt/incident && docker compose ps'
ssh -i ~/.ssh/incident-ec2.pem ubuntu@$IP sudo tail -50 /var/log/cloud-init-output.log  # first-boot log

aws ec2 stop-instances --instance-ids "$INSTANCE_ID"    # stop paying for compute
aws ec2 start-instances --instance-ids "$INSTANCE_ID"   # the stack starts by itself
```

**Stop the instance when you're not using it.** A stopped instance only costs its disk
(within the 30 GB Free Tier). The public IP changes after every stop/start, so look it up again.

**When your own IP changes** (new network, campus Wi-Fi), the firewall will block you. Replace
the rules:

```bash
SG_ID=$(aws ec2 describe-security-groups --group-names incident-ec2 --query 'SecurityGroups[0].GroupId' --output text)
OLD=$(aws ec2 describe-security-groups --group-ids "$SG_ID" --query 'SecurityGroups[0].IpPermissions' --output json)
aws ec2 revoke-security-group-ingress --group-id "$SG_ID" --ip-permissions "$OLD"
MY_IP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --ip-permissions \
  "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$MY_IP/32,Description=ssh}]" \
  "IpProtocol=tcp,FromPort=3000,ToPort=3000,IpRanges=[{CidrIp=$MY_IP/32,Description=dashboard}]"
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
