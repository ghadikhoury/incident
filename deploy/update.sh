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

docker compose up -d --build --wait --remove-orphans
docker image prune -f >/dev/null  # old images would slowly fill the 20 GB disk
docker compose ps
./deploy/verify.sh
