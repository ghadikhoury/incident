#!/bin/bash
# First-boot setup for the EC2 instance (passed as EC2 "user data"; cloud-init runs it
# once, as root). Installs Docker, clones the app, starts it, and registers a boot service.
# Progress and errors: /var/log/cloud-init-output.log on the instance.
set -euxo pipefail

REPO_URL="https://github.com/ghadikhoury/incident.git"
BRANCH="main"
APP_DIR="/opt/incident"

# 2 GB of swap: building the dashboard (Node) can briefly need more than the 2 GB of RAM.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

# Docker Engine + Compose plugin from Docker's official apt repository.
apt-get update
apt-get install -y ca-certificates curl git openssl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
# shellcheck disable=SC1091  # /etc/os-release only exists on the instance
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
  "https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker ubuntu  # lets the ubuntu user run docker without sudo

git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
cat > "$APP_DIR/.env" <<'EOF'
AWS_REGION=us-east-2
# No named profile on EC2: the backend gets credentials from the instance's IAM role.
AWS_PROFILE=
INCIDENT_QUEUE_URL=
INCIDENT_EVIDENCE_BUCKET=
# Add the EC2 layer: ships container logs (and the metrics in them) to CloudWatch.
COMPOSE_FILE=docker-compose.yml:docker-compose.ec2.yml
EOF

"$APP_DIR/deploy/setup-dashboard-auth.sh"

# Start the stack on every boot (e.g. after stopping the instance to save credits).
install -m 0644 "$APP_DIR/deploy/incident.service" /etc/systemd/system/incident.service
systemctl daemon-reload
systemctl enable incident.service

cd "$APP_DIR"
docker compose up -d --build --wait
"$APP_DIR/deploy/verify.sh"
