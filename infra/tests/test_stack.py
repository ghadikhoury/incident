"""Catch infrastructure drift before the CloudFormation stack is deployed."""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template
from infra.stack import IncidentStack


def template() -> Template:
    asset = Path(__file__).resolve().parents[1] / ".build" / "incident-processor.zip"
    app = cdk.App()
    stack = IncidentStack(
        app,
        "TestIncident",
        asset=asset,
        ssh_cidr="203.0.113.7/32",
        key_name="test-key",
    )
    return Template.from_stack(stack)


def test_pipeline_and_retained_data_resources():
    tpl = template()
    for kind, count in {
        "AWS::DynamoDB::Table": 1,
        "AWS::S3::Bucket": 1,
        "AWS::SQS::Queue": 3,
        "AWS::Lambda::Function": 1,
        "AWS::Lambda::EventSourceMapping": 1,
        "AWS::Events::Rule": 1,
        "AWS::CloudWatch::Alarm": 12,
        "AWS::EC2::Instance": 1,
    }.items():
        tpl.resource_count_is(kind, count)
    tpl.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "BillingMode": "PAY_PER_REQUEST",
            "GlobalSecondaryIndexes": Match.array_with(
                [
                    Match.object_like({"IndexName": "by-created"}),
                    Match.object_like({"IndexName": "active-by-created"}),
                ]
            ),
        },
    )


def test_security_boundaries():
    tpl = template()
    tpl.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": Match.any_value(),
        },
    )
    tpl.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {
            "SecurityGroupIngress": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "CidrIp": "203.0.113.7/32",
                }
            ],
        },
    )
    tpl.has_resource_properties(
        "AWS::EC2::Instance",
        {
            "MetadataOptions": {
                "HttpTokens": "required",
                "HttpPutResponseHopLimit": 2,
                "HttpEndpoint": "enabled",
            },
        },
    )


def test_alarm_semantics_and_failure_routes():
    tpl = template()
    alarms = tpl.find_resources("AWS::CloudWatch::Alarm")
    assert {value["Properties"]["AlarmName"] for value in alarms.values()} == {
        f"incident-{service}-{signal}"
        for service in ("gateway", "order", "payment", "inventory")
        for signal in ("latency", "errors", "health")
    }
    tpl.has_resource_properties(
        "AWS::Lambda::EventInvokeConfig",
        {
            "MaximumRetryAttempts": 2,
            "DestinationConfig": {"OnFailure": {"Destination": Match.any_value()}},
        },
    )
    tpl.has_resource_properties(
        "AWS::Events::Rule",
        {
            "Targets": [Match.object_like({"DeadLetterConfig": Match.any_value()})],
        },
    )


def test_disposable_stage_is_isolated_from_live_names():
    asset = Path(__file__).resolve().parents[1] / ".build" / "incident-processor.zip"
    app = cdk.App()
    stage = IncidentStack(
        app,
        "TestVerify",
        asset=asset,
        ssh_cidr="203.0.113.7/32",
        key_name="test-key",
        stage="verify",
        branch="step-10-final",
    )
    resources = Template.from_stack(stage).to_json()["Resources"]
    by_type = {}
    for item in resources.values():
        by_type.setdefault(item["Type"], []).append(item)
    assert by_type["AWS::DynamoDB::Table"][0]["Properties"]["TableName"] == "incident-verify-store"
    assert by_type["AWS::DynamoDB::Table"][0].get("DeletionPolicy", "Delete") == "Delete"
    assert by_type["AWS::SQS::Queue"][0].get("DeletionPolicy", "Delete") == "Delete"
    assert (
        by_type["AWS::Lambda::Function"][0]["Properties"]["FunctionName"]
        == "incident-verify-processor"
    )
    assert by_type["AWS::EC2::Instance"][0]["Properties"]["UserData"]
    names = {item["Properties"]["AlarmName"] for item in by_type["AWS::CloudWatch::Alarm"]}
    assert "incident-verify-payment-latency" in names
    assert not any(name.startswith("incident-payment-") for name in names)
