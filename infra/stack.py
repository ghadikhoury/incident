"""Stage 1 resources with the same names and settings as the manual demo setup."""

import ipaddress
from pathlib import Path

from aws_cdk import CfnOutput, CfnParameter, CfnResource, Fn, RemovalPolicy, Stack
from aws_cdk import aws_s3_assets as assets
from constructs import Construct

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("gateway", "order", "payment", "inventory")
LOG_SERVICES = (*SERVICES, "loadgen", "backend", "frontend", "postgres")
SIGNALS = ("latency", "errors", "health")


def arn(service: str, resource: str) -> str:
    return Fn.sub(
        f"arn:${{AWS::Partition}}:{service}:${{AWS::Region}}:${{AWS::AccountId}}:{resource}"
    )


class IncidentStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        asset: Path,
        ssh_cidr: str,
        key_name: str,
        stage: str = "",
        branch: str = "main",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.stage = stage
        self.prefix = "incident" + ("-" + stage if stage else "")
        self.log_prefix = "/incident" + ("/" + stage if stage else "")
        self.metric_namespace = "Incident" + ("-" + stage if stage else "")
        try:
            ssh_network = ipaddress.ip_network(ssh_cidr, strict=True)
        except ValueError as exc:
            raise ValueError("sshCidr must be a single IPv4 address (/32)") from exc
        if ssh_network.version != 4 or ssh_network.prefixlen != 32:
            raise ValueError("sshCidr must be a single IPv4 address (/32)")

        table = self._resource(
            "Incidents",
            "AWS::DynamoDB::Table",
            {
                "TableName": f"{self.prefix}-store",
                "BillingMode": "PAY_PER_REQUEST",
                "AttributeDefinitions": [
                    {"AttributeName": name, "AttributeType": "S"}
                    for name in ("pk", "sk", "gsi1pk", "gsi1sk", "active_pk")
                ],
                "KeySchema": [
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                "GlobalSecondaryIndexes": [
                    {
                        "IndexName": name,
                        "KeySchema": [
                            {"AttributeName": partition, "KeyType": "HASH"},
                            {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                    for name, partition in (
                        ("by-created", "gsi1pk"),
                        ("active-by-created", "active_pk"),
                    )
                ],
                "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
            },
            retain=not stage,
        )

        bucket_name = Fn.sub(f"{self.prefix}-evidence-${{AWS::AccountId}}")
        bucket = self._resource(
            "Evidence",
            "AWS::S3::Bucket",
            {
                "BucketName": bucket_name,
                "BucketEncryption": {
                    "ServerSideEncryptionConfiguration": [
                        {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                    ]
                },
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
                "LifecycleConfiguration": {
                    "Rules": [
                        {
                            "Id": "ExpireDemoEvidence",
                            "Status": "Enabled",
                            "Prefix": "incidents/",
                            "ExpirationInDays": 30,
                        }
                    ]
                },
            },
            retain=not stage,
        )
        self._resource(
            "EvidenceHttpsOnly",
            "AWS::S3::BucketPolicy",
            {
                "Bucket": bucket.ref,
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "DenyInsecureTransport",
                            "Effect": "Deny",
                            "Principal": "*",
                            "Action": "s3:*",
                            "Resource": [
                                Fn.get_att(bucket.logical_id, "Arn"),
                                Fn.get_att(bucket.logical_id, "Arn").to_string() + "/*",
                            ],
                            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                        }
                    ],
                },
            },
        )

        updates = self._queue("Updates", f"{self.prefix}-updates", 4, 90, wait=10)
        failures = self._queue("Failures", f"{self.prefix}-pipeline-failures", 14, 30)
        backfill = self._queue(
            "Backfill",
            f"{self.prefix}-evidence-backfill",
            4,
            270,
            redrive={
                "deadLetterTargetArn": Fn.get_att(failures.logical_id, "Arn"),
                "maxReceiveCount": 5,
            },
        )

        logs = {}
        for service in LOG_SERVICES:
            logs[service] = self._resource(
                f"Log{service.title()}",
                "AWS::Logs::LogGroup",
                {
                    "LogGroupName": f"{self.log_prefix}/{service}",
                    "RetentionInDays": 14,
                },
                retain=not stage,
            )
        lambda_log = self._resource(
            "ProcessorLog",
            "AWS::Logs::LogGroup",
            {
                "LogGroupName": f"/aws/lambda/{self.prefix}-processor",
                "RetentionInDays": 14,
            },
            retain=not stage,
        )

        vpc = self._resource(
            "Vpc",
            "AWS::EC2::VPC",
            {
                "CidrBlock": "10.42.0.0/16",
                "EnableDnsHostnames": True,
                "EnableDnsSupport": True,
            },
        )
        gateway = self._resource("InternetGateway", "AWS::EC2::InternetGateway", {})
        attachment = self._resource(
            "GatewayAttachment",
            "AWS::EC2::VPCGatewayAttachment",
            {
                "VpcId": vpc.ref,
                "InternetGatewayId": gateway.ref,
            },
        )
        subnet = self._resource(
            "PublicSubnet",
            "AWS::EC2::Subnet",
            {
                "VpcId": vpc.ref,
                "CidrBlock": "10.42.0.0/24",
                "MapPublicIpOnLaunch": True,
            },
        )
        routes = self._resource("PublicRoutes", "AWS::EC2::RouteTable", {"VpcId": vpc.ref})
        route = self._resource(
            "InternetRoute",
            "AWS::EC2::Route",
            {
                "RouteTableId": routes.ref,
                "DestinationCidrBlock": "0.0.0.0/0",
                "GatewayId": gateway.ref,
            },
        )
        route.add_resource_dependency(attachment)
        self._resource(
            "SubnetRoute",
            "AWS::EC2::SubnetRouteTableAssociation",
            {
                "SubnetId": subnet.ref,
                "RouteTableId": routes.ref,
            },
        )
        security_group = self._resource(
            "SshOnly",
            "AWS::EC2::SecurityGroup",
            {
                "GroupDescription": "Incident SSH access from one address",
                "VpcId": vpc.ref,
                "SecurityGroupIngress": [
                    {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "CidrIp": ssh_cidr}
                ],
                "SecurityGroupEgress": [{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}],
            },
        )

        role = self._role(
            "InstanceRole",
            f"{self.prefix}-cdk-ec2",
            "ec2.amazonaws.com",
            [
                self._allow(
                    [
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:ConditionCheckItem",
                        "dynamodb:Query",
                    ],
                    [
                        Fn.get_att(table.logical_id, "Arn"),
                        Fn.get_att(table.logical_id, "Arn").to_string() + "/index/*",
                    ],
                ),
                self._allow(
                    ["sqs:ReceiveMessage", "sqs:DeleteMessage"],
                    Fn.get_att(updates.logical_id, "Arn"),
                ),
                self._allow(
                    "s3:GetObject",
                    Fn.get_att(bucket.logical_id, "Arn").to_string() + "/incidents/*",
                ),
                self._allow("cloudwatch:GetMetricData", "*"),
                self._allow("logs:StartQuery", arn("logs", f"log-group:{self.log_prefix}/*")),
                self._allow("logs:GetQueryResults", "*"),
                self._allow(
                    "bedrock:InvokeModel",
                    Fn.sub(
                        "arn:${AWS::Partition}:bedrock:${AWS::Region}::foundation-model/openai.gpt-oss-20b-1:0"
                    ),
                ),
                self._allow(
                    ["logs:CreateLogStream", "logs:PutLogEvents"],
                    [
                        arn("logs", f"log-group:{self.log_prefix}/*"),
                        arn("logs", f"log-group:{self.log_prefix}/*:log-stream:*"),
                    ],
                ),
            ],
        )
        profile = self._resource(
            "InstanceProfile",
            "AWS::IAM::InstanceProfile",
            {
                "Roles": [role.ref],
            },
        )
        ami = CfnParameter(
            self,
            "UbuntuAmi",
            type="AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>",
            default="/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
        )
        user_data = (ROOT / "deploy" / "user-data.sh").read_text(encoding="utf-8")
        user_data = user_data.replace(
            "INCIDENT_QUEUE_URL=\n", "INCIDENT_QUEUE_URL=${IncidentQueueUrl}\n"
        ).replace(
            "INCIDENT_EVIDENCE_BUCKET=\n",
            "INCIDENT_EVIDENCE_BUCKET=${IncidentEvidenceBucket}\n",
        )
        user_data = user_data.replace('BRANCH="main"', 'BRANCH="${IncidentBranch}"')
        user_data = user_data.replace(
            "INCIDENT_QUEUE_URL=${IncidentQueueUrl}\n",
            "INCIDENT_QUEUE_URL=${IncidentQueueUrl}\n"
            "INCIDENT_TABLE=${IncidentTable}\n"
            "INCIDENT_LOG_PREFIX=${IncidentLogPrefix}\n"
            "INCIDENT_METRIC_NAMESPACE=${IncidentMetricNamespace}\n"
            "INCIDENT_ALARM_PREFIX=${IncidentAlarmPrefix}\n",
        )
        user_data = Fn.sub(
            user_data,
            {
                "IncidentQueueUrl": updates.ref,
                "IncidentEvidenceBucket": bucket.ref,
                "IncidentTable": table.ref,
                "IncidentLogPrefix": self.log_prefix,
                "IncidentMetricNamespace": self.metric_namespace,
                "IncidentAlarmPrefix": self.prefix,
                "IncidentBranch": branch,
            },
        )
        instance = self._resource(
            "DemoInstance",
            "AWS::EC2::Instance",
            {
                "ImageId": ami.value_as_string,
                "InstanceType": "t3.small",
                "KeyName": key_name,
                "SubnetId": subnet.ref,
                "SecurityGroupIds": [security_group.ref],
                "IamInstanceProfile": profile.ref,
                "MetadataOptions": {
                    "HttpTokens": "required",
                    "HttpPutResponseHopLimit": 2,
                    "HttpEndpoint": "enabled",
                },
                "BlockDeviceMappings": [
                    {
                        "DeviceName": "/dev/sda1",
                        "Ebs": {
                            "VolumeSize": 20,
                            "VolumeType": "gp3",
                            "Encrypted": True,
                            "DeleteOnTermination": True,
                        },
                    }
                ],
                "UserData": Fn.base64(user_data),
                "Tags": [
                    {"Key": "Name", "Value": self.prefix},
                    {"Key": "Project", "Value": "incident"},
                ],
            },
        )
        instance.add_resource_dependency(route)
        for group in logs.values():
            instance.add_resource_dependency(group)

        processor_role = self._role(
            "ProcessorRole",
            f"{self.prefix}-cdk-processor",
            "lambda.amazonaws.com",
            [
                self._allow(
                    [
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:ConditionCheckItem",
                    ],
                    Fn.get_att(table.logical_id, "Arn"),
                ),
                self._allow(
                    "s3:PutObject",
                    Fn.get_att(bucket.logical_id, "Arn").to_string() + "/incidents/*",
                ),
                self._allow(
                    "sqs:SendMessage",
                    [Fn.get_att(q.logical_id, "Arn") for q in (updates, backfill, failures)],
                ),
                self._allow(
                    ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
                    Fn.get_att(backfill.logical_id, "Arn"),
                ),
                self._allow(
                    ["logs:StartQuery", "logs:GetQueryResults"],
                    [
                        arn("logs", f"log-group:{self.log_prefix}/{service}:*")
                        for service in (*SERVICES, "backend")
                    ],
                ),
                self._allow(
                    [
                        "cloudwatch:DescribeAlarms",
                        "cloudwatch:GetMetricData",
                        "ec2:DescribeInstances",
                    ],
                    "*",
                ),
                self._allow(
                    ["logs:CreateLogStream", "logs:PutLogEvents"],
                    arn("logs", f"log-group:/aws/lambda/{self.prefix}-processor:*"),
                ),
            ],
        )
        code = assets.Asset(self, "ProcessorAsset", path=str(asset))
        processor = self._resource(
            "Processor",
            "AWS::Lambda::Function",
            {
                "FunctionName": f"{self.prefix}-processor",
                "Runtime": "python3.12",
                "Handler": "incident_api.pipeline.lambda_handler",
                "Role": Fn.get_att(processor_role.logical_id, "Arn"),
                "Code": {"S3Bucket": code.s3_bucket_name, "S3Key": code.s3_object_key},
                "Timeout": 45,
                "MemorySize": 256,
                "Environment": {
                    "Variables": {
                        "INCIDENT_EVIDENCE_BUCKET": bucket.ref,
                        "INCIDENT_QUEUE_URL": updates.ref,
                        "INCIDENT_BACKFILL_QUEUE_URL": backfill.ref,
                        "INCIDENT_INSTANCE_ID": instance.ref,
                        "INCIDENT_TABLE": table.ref,
                        "INCIDENT_LOG_PREFIX": self.log_prefix,
                        "INCIDENT_METRIC_NAMESPACE": self.metric_namespace,
                        "INCIDENT_ALARM_PREFIX": self.prefix,
                    }
                },
            },
        )
        processor.add_resource_dependency(lambda_log)
        self._resource(
            "ProcessorFailures",
            "AWS::Lambda::EventInvokeConfig",
            {
                "FunctionName": processor.ref,
                "Qualifier": "$LATEST",
                "MaximumRetryAttempts": 2,
                "DestinationConfig": {
                    "OnFailure": {"Destination": Fn.get_att(failures.logical_id, "Arn")}
                },
            },
        )
        self._resource(
            "BackfillConsumer",
            "AWS::Lambda::EventSourceMapping",
            {
                "EventSourceArn": Fn.get_att(backfill.logical_id, "Arn"),
                "FunctionName": processor.ref,
                "BatchSize": 1,
                "Enabled": True,
            },
        )

        alarm_names = []
        for service in SERVICES:
            for signal in SIGNALS:
                name = f"{self.prefix}-{service}-{signal}"
                alarm_names.append(name)
                self._alarm(service, signal, name)
        rule = self._resource(
            "AlarmTransitions",
            "AWS::Events::Rule",
            {
                "Name": f"{self.prefix}-alarm-transitions",
                "State": "ENABLED",
                "EventPattern": {
                    "source": ["aws.cloudwatch"],
                    "detail-type": ["CloudWatch Alarm State Change"],
                    "detail": {
                        "alarmName": alarm_names,
                        "state": {"value": ["ALARM", "OK", "INSUFFICIENT_DATA"]},
                    },
                },
                "Targets": [
                    {
                        "Id": f"{self.prefix}-processor",
                        "Arn": Fn.get_att(processor.logical_id, "Arn"),
                        "DeadLetterConfig": {"Arn": Fn.get_att(failures.logical_id, "Arn")},
                    }
                ],
            },
        )
        self._resource(
            "AllowAlarmRule",
            "AWS::Lambda::Permission",
            {
                "Action": "lambda:InvokeFunction",
                "FunctionName": processor.ref,
                "Principal": "events.amazonaws.com",
                "SourceArn": Fn.get_att(rule.logical_id, "Arn"),
            },
        )
        self._resource(
            "FailureQueuePolicy",
            "AWS::SQS::QueuePolicy",
            {
                "Queues": [failures.ref],
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "events.amazonaws.com"},
                            "Action": "sqs:SendMessage",
                            "Resource": Fn.get_att(failures.logical_id, "Arn"),
                            "Condition": {
                                "ArnEquals": {"aws:SourceArn": Fn.get_att(rule.logical_id, "Arn")}
                            },
                        }
                    ],
                },
            },
        )

        CfnOutput(self, "InstanceId", value=instance.ref)
        CfnOutput(self, "PublicIp", value=Fn.get_att(instance.logical_id, "PublicIp").to_string())
        CfnOutput(self, "UpdatesQueueUrl", value=updates.ref)
        CfnOutput(self, "EvidenceBucket", value=bucket.ref)

    def _resource(
        self, ident: str, resource_type: str, props: dict, *, retain: bool = False
    ) -> CfnResource:
        resource = CfnResource(self, ident, type=resource_type, properties=props)
        if retain:
            resource.apply_removal_policy(RemovalPolicy.RETAIN)
        return resource

    def _queue(
        self,
        ident: str,
        name: str,
        retention_days: int,
        visibility_seconds: int,
        *,
        wait: int = 0,
        redrive: dict | None = None,
    ) -> CfnResource:
        props = {
            "QueueName": name,
            "MessageRetentionPeriod": retention_days * 86400,
            "VisibilityTimeout": visibility_seconds,
            "SqsManagedSseEnabled": True,
        }
        if wait:
            props["ReceiveMessageWaitTimeSeconds"] = wait
        if redrive:
            props["RedrivePolicy"] = redrive
        return self._resource(ident, "AWS::SQS::Queue", props, retain=not self.stage)

    def _role(self, ident: str, name: str, principal: str, statements: list[dict]) -> CfnResource:
        return self._resource(
            ident,
            "AWS::IAM::Role",
            {
                "RoleName": name,
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": principal},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "incident-runtime",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": statements,
                        },
                    }
                ],
            },
        )

    @staticmethod
    def _allow(actions: str | list[str], resources: str | list[str]) -> dict:
        return {"Effect": "Allow", "Action": actions, "Resource": resources}

    def _alarm(self, service: str, signal: str, name: str) -> None:
        props = {
            "AlarmName": name,
            "EvaluationPeriods": 3,
            "DatapointsToAlarm": 2,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "missing" if signal == "health" else "notBreaching",
        }
        dimension = [{"Name": "Service", "Value": service}]
        if signal == "errors":
            props.update(
                {
                    "Threshold": 20,
                    "Metrics": [
                        {
                            "Id": metric.lower(),
                            "ReturnData": False,
                            "MetricStat": {
                                "Metric": {
                                    "Namespace": self.metric_namespace,
                                    "MetricName": metric,
                                    "Dimensions": dimension,
                                },
                                "Period": 60,
                                "Stat": "Sum",
                            },
                        }
                        for metric in ("Errors", "Requests")
                    ]
                    + [
                        {
                            "Id": "error_rate",
                            "Expression": "IF(requests > 0, 100 * errors / requests, 0)",
                            "Label": "5xx error rate (%)",
                            "ReturnData": True,
                        }
                    ],
                }
            )
        else:
            props.update(
                {
                    "Namespace": self.metric_namespace,
                    "MetricName": "Latency" if signal == "latency" else "HealthCheckFailed",
                    "Dimensions": dimension,
                    "Period": 60,
                    "Threshold": 2000 if signal == "latency" else 0.5,
                }
            )
            if signal == "latency":
                props["ExtendedStatistic"] = "p90"
            else:
                props["Statistic"] = "Average"
        self._resource(f"Alarm{service.title()}{signal.title()}", "AWS::CloudWatch::Alarm", props)
