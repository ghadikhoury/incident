# Step 10 fresh deployment verification

On 2026-10-03, PR #13's `step-10-final` branch was deployed as the isolated
`IncidentStage1-verify` CDK stack in us-east-2. The verification stage used
separate resource names, log groups, metrics, IAM roles, VPC, and instance;
the existing CLI demo remained online.

- `cdk bootstrap` completed, then `cdk deploy` created the stack and published
  the Lambda asset. CloudFormation reached `CREATE_COMPLETE`.
- On the new EC2 instance, cloud-init completed and
  `sudo /opt/incident/deploy/verify.sh` reported the dashboard, DynamoDB API,
  and all services healthy.
- The `db_slow` scenario created `INC-1002` in **125.2 seconds**, grouped
  **7 alarms** into one incident, selected **payment** as the probable root,
  recovered, and reported no error. The saved result was checked on the
  instance before teardown. Bedrock returned `UNAVAILABLE` because of the
  account's existing model quota, so AI diagnosis quality was not verified.
- All three PR CI jobs passed after the fix: Python, frontend, and Compose
  smoke. The Python job includes ShellCheck and the CDK tests.
- The stage evidence bucket was emptied and `cdk destroy` completed. A late
  evidence backfill wrote to the bucket during the first destroy attempt;
  emptying the stage bucket again and retrying completed deletion. The stage
  stack, table, and bucket were confirmed absent. The original `incident-store`
  table, evidence bucket, and running demo instance were confirmed present.

The default CDK stack has not replaced the original CLI resources. That data
migration still needs an approved export and restore plan before a default
`cdk deploy` in this account. The CDK bootstrap toolkit remains available for
future deployments.
