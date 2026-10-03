#!/bin/bash
# Creates Incident's CloudWatch log groups and alarms. Safe to rerun: existing log groups are
# kept and alarms are updated in place, so changing a threshold here and rerunning applies it.
# Run from your laptop (the instance role deliberately can't do this):
#   AWS_PROFILE=incident deploy/setup-cloudwatch.sh
set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-2}"
NAMESPACE="Incident"
RETENTION_DAYS=14
LOG_GROUPS=(gateway order payment inventory loadgen backend frontend postgres)
MONITORED=(gateway order payment inventory)

# Alarm thresholds. Each alarm looks at 1-minute periods and fires when 2 of the last 3 breach,
# so one noisy minute doesn't page anyone but a real problem is caught within ~2-3 minutes.
LATENCY_P90_MS=2000
ERROR_RATE_PERCENT=20
HEALTH_FAILED_FRACTION=0.5
PERIOD=60
EVALUATION_PERIODS=3
DATAPOINTS_TO_ALARM=2

for name in "${LOG_GROUPS[@]}"; do
  group="/incident/$name"
  existing=$(aws logs describe-log-groups --log-group-name-prefix "$group" \
    --query "length(logGroups[?logGroupName=='$group'])" --output text)
  if [ "$existing" = "0" ]; then
    aws logs create-log-group --log-group-name "$group" --tags Project=incident
  fi
  aws logs put-retention-policy --log-group-name "$group" --retention-in-days "$RETENTION_DAYS"
  echo "log group $group (retention ${RETENTION_DAYS}d)"
done

# Settings shared by every alarm. Missing data (e.g. no traffic at night) is not a problem.
# (--period is passed separately: alarms built from --metrics carry it per metric instead.)
common=(
  --evaluation-periods "$EVALUATION_PERIODS"
  --datapoints-to-alarm "$DATAPOINTS_TO_ALARM"
  --comparison-operator GreaterThanThreshold
  --treat-missing-data notBreaching
  --tags "Key=Project,Value=incident"
)

metric_stat() {  # metric_stat <id> <metric> <service> <stat>
  printf '{"Id":"%s","ReturnData":false,"MetricStat":{"Metric":{"Namespace":"%s","MetricName":"%s","Dimensions":[{"Name":"Service","Value":"%s"}]},"Period":%s,"Stat":"%s"}}' \
    "$1" "$NAMESPACE" "$2" "$3" "$PERIOD" "$4"
}

for service in "${MONITORED[@]}"; do
  aws cloudwatch put-metric-alarm "${common[@]}" \
    --alarm-name "incident-$service-latency" \
    --alarm-description "$service: p90 latency above ${LATENCY_P90_MS} ms" \
    --namespace "$NAMESPACE" --metric-name Latency \
    --dimensions "Name=Service,Value=$service" --period "$PERIOD" \
    --extended-statistic p90 --threshold "$LATENCY_P90_MS"

  # Error rate = 5xx / all requests, in percent. IF() avoids dividing by zero.
  aws cloudwatch put-metric-alarm "${common[@]}" \
    --alarm-name "incident-$service-errors" \
    --alarm-description "$service: more than ${ERROR_RATE_PERCENT}% of requests fail with 5xx" \
    --threshold "$ERROR_RATE_PERCENT" \
    --metrics "[$(metric_stat errors Errors "$service" Sum),$(metric_stat requests Requests "$service" Sum),{\"Id\":\"error_rate\",\"Expression\":\"IF(requests > 0, 100 * errors / requests, 0)\",\"Label\":\"5xx error rate (%)\",\"ReturnData\":true}]"

  # Average of 0/1 per health check = fraction of failed checks in the period.
  aws cloudwatch put-metric-alarm "${common[@]}" \
    --alarm-name "incident-$service-health" \
    --alarm-description "$service: more than half of its health checks failed (down or unhealthy)" \
    --namespace "$NAMESPACE" --metric-name HealthCheckFailed \
    --dimensions "Name=Service,Value=$service" --period "$PERIOD" \
    --statistic Average --threshold "$HEALTH_FAILED_FRACTION"

  echo "alarms incident-$service-{latency,errors,health}"
done
