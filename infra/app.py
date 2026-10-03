"""CDK entry point. Run `python -m infra.build` before `cdk synth/deploy`."""

import re
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
stage = app.node.try_get_context("stage") or ""
branch = app.node.try_get_context("branch") or "main"
if stage and not re.fullmatch(r"[a-z][a-z0-9]{0,11}", stage):
    raise SystemExit("stage must be 1-12 lowercase letters/digits, starting with a letter")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}", branch):
    raise SystemExit("branch contains unsupported characters")
if not ssh_cidr or not key_name:
    raise SystemExit("Pass -c sshCidr=<your-ip>/32 -c keyName=<existing-key-pair>")

IncidentStack(
    app,
    "IncidentStage1" + ("-" + stage if stage else ""),
    asset=asset,
    ssh_cidr=ssh_cidr,
    key_name=key_name,
    stage=stage,
    branch=branch,
)
app.synth()
