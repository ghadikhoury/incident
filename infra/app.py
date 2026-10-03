"""CDK entry point. Run `python -m infra.build` before `cdk synth/deploy`."""

import sys
from pathlib import Path

import aws_cdk as cdk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.stack import IncidentStack

asset = Path(__file__).resolve().parent / ".build" / "incident-processor.zip"
if not asset.is_file():
    raise SystemExit("Run `python -m infra.build` before cdk synth/deploy")

app = cdk.App()
ssh_cidr = app.node.try_get_context("sshCidr")
key_name = app.node.try_get_context("keyName")
if not ssh_cidr or not key_name:
    raise SystemExit("Pass -c sshCidr=<your-ip>/32 -c keyName=<existing-key-pair>")

IncidentStack(app, "IncidentStage1", asset=asset, ssh_cidr=ssh_cidr, key_name=key_name)
app.synth()
