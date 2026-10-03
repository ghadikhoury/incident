#!/bin/bash
# End-to-end check of a deployment, run on the instance (user-data.sh and update.sh call it):
#   /opt/incident/deploy/verify.sh
# Goes through nginx like a browser would, and does a real DynamoDB read, so a missing
# table/index or missing IAM permissions fail here instead of only when someone uses the UI.
set -euo pipefail

API="${API_URL:-http://127.0.0.1:3000/api}"  # API_URL: check a different deployment

curl -fsS "$API/health" >/dev/null

if ! curl -fsS -o /dev/null "$API/incidents?active=true"; then
  echo "FAIL: incident API can't read DynamoDB. Is incident-store set up (docs/DEPLOY.md," \
    "step 0) and does the instance role allow it?" >&2
  exit 1
fi

# The health monitor polls every 3 s; give freshly started services up to a minute.
all_healthy='import json, sys; sys.exit(any(s["status"] != "healthy" for s in json.load(sys.stdin)))'
for _ in $(seq 1 30); do
  if curl -fsS "$API/services" | python3 -c "$all_healthy"; then
    echo "OK: dashboard, incident API (DynamoDB) and all services healthy"
    exit 0
  fi
  sleep 2
done
echo "FAIL: not all services became healthy:" >&2
curl -fsS "$API/services" >&2
exit 1
