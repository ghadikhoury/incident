#!/bin/bash
# Create one random dashboard credential on the host. Never put it in Git, user data,
# CloudFormation parameters, or container environment variables.
set -euo pipefail

install -d -m 0700 /etc/incident
if [ ! -s /etc/incident/dashboard-credentials ]; then
  password=$(openssl rand -hex 24)
  printf 'incident:%s\n' "$password" > /etc/incident/dashboard-credentials
  chmod 0600 /etc/incident/dashboard-credentials
fi
IFS=: read -r username password < /etc/incident/dashboard-credentials
if [ "$username" != incident ] || [ -z "$password" ]; then
  echo 'Invalid dashboard credential file' >&2
  exit 1
fi
printf '%s\n' "$password" | openssl passwd -apr1 -stdin | {
  IFS= read -r hash
  printf 'incident:%s\n' "$hash" > /etc/incident/dashboard.htpasswd
}
chmod 0644 /etc/incident/dashboard.htpasswd
printf 'machine 127.0.0.1 login incident password %s\n' "$password" \
  > /etc/incident/dashboard.netrc
chmod 0600 /etc/incident/dashboard.netrc
