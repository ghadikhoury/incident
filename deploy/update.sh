#!/bin/bash
# Deploy the latest code on the instance:  sudo /opt/incident/deploy/update.sh [branch]
# Rebuilds only what changed; containers whose image and config are unchanged keep running.
set -euo pipefail

BRANCH="${1:-main}"
cd /opt/incident

git fetch --quiet origin
git checkout --quiet "$BRANCH"
git pull --quiet --ff-only origin "$BRANCH"
echo "deploying $(git log -1 --format='%h %s')"

# An updater launched from the pre-auth release can check out this branch but
# continue running its old shell body. Docker then creates a directory for the
# missing bind-mounted credential file. Recover it on the next invocation.
auth_rebind=0
if [ -d /etc/incident/dashboard.htpasswd ]; then
  rmdir /etc/incident/dashboard.htpasswd  # only an empty accidental mount path
  auth_rebind=1
fi
./deploy/setup-dashboard-auth.sh

docker compose up -d --build --wait --remove-orphans
if [ "$auth_rebind" -eq 1 ]; then
  docker compose up -d --no-deps --force-recreate --wait frontend
fi
docker image prune -f >/dev/null  # old images would slowly fill the 20 GB disk
docker compose ps
./deploy/verify.sh
