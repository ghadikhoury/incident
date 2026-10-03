"""Apply the EC2 backend IAM policy for diagnosis and incident telemetry.

Run: AWS_PROFILE=incident python deploy/setup_diagnosis.py
"""

import json
from pathlib import Path

import boto3

REGION = "us-east-2"
POLICY = Path(__file__).resolve().parent / "iam" / "instance-policy.json"


def main() -> None:
    session = boto3.Session(region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    document = json.loads(POLICY.read_text().replace("ACCOUNT_ID", account))
    session.client("iam").put_role_policy(
        RoleName="incident-ec2",
        PolicyName="incident-instance",
        PolicyDocument=json.dumps(document),
    )
    print("Updated incident-ec2 role for evidence, metrics, log search, and Bedrock")


if __name__ == "__main__":
    main()
