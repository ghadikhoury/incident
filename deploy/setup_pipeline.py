"""Provision Step 6 resources and publish the Lambda from this checkout.

Run: AWS_PROFILE=incident python deploy/setup_pipeline.py --instance-id i-...
Requires the existing table, log groups, alarms, and EC2 instance role.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
REGION = "us-east-2"
ROLE = "incident-processor"
FUNCTION = "incident-processor"
QUEUE = "incident-updates"
BACKFILL_QUEUE = "incident-evidence-backfill"
FAILURE_QUEUE = "incident-pipeline-failures"


def _policy(statements):
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def _statement(sid, actions, resources):
    return {"Sid": sid, "Effect": "Allow", "Action": actions, "Resource": resources}


def _package():
    """Build Linux x86_64 Pydantic wheels even when invoked from Windows."""
    with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
        target = Path(temporary)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--target",
                str(target),
                "--platform",
                "manylinux2014_x86_64",
                "--python-version",
                "3.12",
                "--implementation",
                "cp",
                "--only-binary=:all:",
                "pydantic==2.13.5",
            ],
            check=True,
        )
        source = ROOT / "backend" / "incident_api"
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(target.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    bundle.write(path, path.relative_to(target).as_posix())
            for name in ("__init__.py", "config.py", "models.py", "store.py", "pipeline.py"):
                bundle.write(source / name, f"incident_api/{name}")
        return archive.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args()
    session = boto3.Session(region_name=REGION, profile_name=os.getenv("AWS_PROFILE") or None)
    account = session.client("sts").get_caller_identity()["Account"]
    reservations = session.client("ec2").describe_instances(InstanceIds=[args.instance_id])[
        "Reservations"
    ]
    if not any(
        instance["State"]["Name"] in {"running", "stopped"}
        for reservation in reservations
        for instance in reservation["Instances"]
    ):
        parser.error("instance must be running or stopped in us-east-2")
    package = _package()
    s3 = session.client("s3")
    sqs = session.client("sqs")
    iam = session.client("iam")
    lam = session.client("lambda")
    events = session.client("events")
    logs = session.client("logs")
    bucket = f"incident-evidence-{account}"
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "ExpireDemoEvidence",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "incidents/"},
                    "Expiration": {"Days": 30},
                }
            ]
        },
    )
    queue_url = sqs.create_queue(
        QueueName=QUEUE,
        Attributes={
            "VisibilityTimeout": "90",
            "ReceiveMessageWaitTimeSeconds": "10",
            "MessageRetentionPeriod": "345600",
        },
    )["QueueUrl"]
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={"SqsManagedSseEnabled": "true"})
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    failure_url = sqs.create_queue(
        QueueName=FAILURE_QUEUE,
        Attributes={"MessageRetentionPeriod": "1209600", "SqsManagedSseEnabled": "true"},
    )["QueueUrl"]
    failure_arn = sqs.get_queue_attributes(QueueUrl=failure_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    backfill_url = sqs.create_queue(
        QueueName=BACKFILL_QUEUE,
        Attributes={
            "VisibilityTimeout": "270",  # six times the Lambda's 45-second timeout
            "MessageRetentionPeriod": "345600",
            "SqsManagedSseEnabled": "true",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": failure_arn, "maxReceiveCount": 5}),
        },
    )["QueueUrl"]
    backfill_arn = sqs.get_queue_attributes(QueueUrl=backfill_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    trust_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        role_arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(
            RoleName=ROLE,
            AssumeRolePolicyDocument=json.dumps(trust_doc),
            Description="Processes Incident CloudWatch alarm transitions",
        )["Role"]["Arn"]
    table_arn = f"arn:aws:dynamodb:{REGION}:{account}:table/incident-store"
    log_groups = [
        f"arn:aws:logs:{REGION}:{account}:log-group:/incident/{name}:*"
        for name in ("gateway", "order", "payment", "inventory", "backend")
    ]
    policy = _policy(
        [
            _statement(
                "IncidentTable",
                [
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:ConditionCheckItem",
                ],
                table_arn,
            ),
            _statement("EvidenceObjects", "s3:PutObject", f"arn:aws:s3:::{bucket}/incidents/*"),
            _statement(
                "SendUpdatesAndBackfill", "sqs:SendMessage", [queue_arn, backfill_arn, failure_arn]
            ),
            _statement(
                "ReadBackfill",
                ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
                backfill_arn,
            ),
            _statement(
                "QueryIncidentLogs", ["logs:StartQuery", "logs:GetQueryResults"], log_groups
            ),
            # These CloudWatch read APIs require a wildcard resource.
            _statement(
                "ReadAlarmAndMetrics",
                ["cloudwatch:DescribeAlarms", "cloudwatch:GetMetricData"],
                "*",
            ),
            _statement("ReadDemoInstanceState", "ec2:DescribeInstances", "*"),
            _statement(
                "LambdaLogging",
                ["logs:CreateLogStream", "logs:PutLogEvents"],
                f"arn:aws:logs:{REGION}:{account}:log-group:/aws/lambda/{FUNCTION}:*",
            ),
        ]
    )
    iam.put_role_policy(RoleName=ROLE, PolicyName="incident-pipeline", PolicyDocument=policy)
    ec2_policy = (
        (ROOT / "deploy" / "iam" / "instance-policy.json")
        .read_text()
        .replace("ACCOUNT_ID", account)
    )
    iam.put_role_policy(
        RoleName="incident-ec2", PolicyName="incident-instance", PolicyDocument=ec2_policy
    )
    try:
        logs.create_log_group(logGroupName=f"/aws/lambda/{FUNCTION}")
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=f"/aws/lambda/{FUNCTION}", retentionInDays=14)
    environment = {
        "Variables": {
            "INCIDENT_EVIDENCE_BUCKET": bucket,
            "INCIDENT_QUEUE_URL": queue_url,
            "INCIDENT_BACKFILL_QUEUE_URL": backfill_url,
            "INCIDENT_INSTANCE_ID": args.instance_id,
        }
    }
    try:
        lam.get_function(FunctionName=FUNCTION)
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(12):
            try:
                lam.create_function(
                    FunctionName=FUNCTION,
                    Runtime="python3.12",
                    Role=role_arn,
                    Handler="incident_api.pipeline.lambda_handler",
                    Code={"ZipFile": package},
                    Timeout=45,
                    MemorySize=256,
                    Environment=environment,
                    Architectures=["x86_64"],
                )
                break
            except ClientError as exc:
                if (
                    exc.response["Error"]["Code"] != "InvalidParameterValueException"
                    or attempt == 11
                ):
                    raise
                time.sleep(5)  # new IAM roles take a short time to propagate
    else:
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=package, Publish=True)
        lam.get_waiter("function_updated").wait(FunctionName=FUNCTION)
        lam.update_function_configuration(
            FunctionName=FUNCTION,
            Runtime="python3.12",
            Role=role_arn,
            Handler="incident_api.pipeline.lambda_handler",
            Timeout=45,
            MemorySize=256,
            Environment=environment,
        )
    lam.get_waiter("function_active").wait(FunctionName=FUNCTION)
    lam.get_waiter("function_updated").wait(FunctionName=FUNCTION)
    lam.put_function_event_invoke_config(
        FunctionName=FUNCTION,
        MaximumRetryAttempts=2,
        DestinationConfig={"OnFailure": {"Destination": failure_arn}},
    )
    mappings = lam.list_event_source_mappings(FunctionName=FUNCTION, EventSourceArn=backfill_arn)[
        "EventSourceMappings"
    ]
    if not mappings:
        lam.create_event_source_mapping(
            EventSourceArn=backfill_arn, FunctionName=FUNCTION, BatchSize=1, Enabled=True
        )
    alarm_names = [
        f"incident-{service}-{signal}"
        for service in ("gateway", "order", "payment", "inventory")
        for signal in ("latency", "errors", "health")
    ]
    rule_arn = events.put_rule(
        Name="incident-alarm-transitions",
        State="ENABLED",
        EventPattern=json.dumps(
            {
                "source": ["aws.cloudwatch"],
                "detail-type": ["CloudWatch Alarm State Change"],
                "detail": {
                    "alarmName": alarm_names,
                    "state": {"value": ["ALARM", "OK", "INSUFFICIENT_DATA"]},
                },
            }
        ),
    )["RuleArn"]
    sqs.set_queue_attributes(
        QueueUrl=failure_url,
        Attributes={
            "Policy": _policy(
                [
                    {
                        "Sid": "EventBridgeFailedAlarmDelivery",
                        "Effect": "Allow",
                        "Principal": {"Service": "events.amazonaws.com"},
                        "Action": "sqs:SendMessage",
                        "Resource": failure_arn,
                        "Condition": {"ArnEquals": {"aws:SourceArn": rule_arn}},
                    }
                ]
            )
        },
    )
    try:
        lam.add_permission(
            FunctionName=FUNCTION,
            StatementId="IncidentAlarmRule",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
    except lam.exceptions.ResourceConflictException:
        pass
    targets = events.put_targets(
        Rule="incident-alarm-transitions",
        Targets=[
            {
                "Id": "incident-processor",
                "Arn": f"arn:aws:lambda:{REGION}:{account}:function:{FUNCTION}",
                "DeadLetterConfig": {"Arn": failure_arn},
            }
        ],
    )
    if targets["FailedEntryCount"]:
        raise RuntimeError(f"EventBridge target failed: {targets['FailedEntries']}")
    print(
        f"Evidence bucket: {bucket}\nSQS updates: {queue_url}\n"
        f"SQS backfill: {backfill_url}\nSQS failures: {failure_url}\n"
        f"Lambda: {FUNCTION}\nRule: {rule_arn}"
    )
    print("Add INCIDENT_QUEUE_URL to the EC2 .env and redeploy the backend branch.")


if __name__ == "__main__":
    main()
