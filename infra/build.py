"""Build the Linux Lambda asset used by CDK before synth/deploy."""

from pathlib import Path

from deploy.setup_pipeline import _package

ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "infra" / ".build" / "incident-processor.zip"


def main() -> None:
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    ASSET.write_bytes(_package())
    print(ASSET)


if __name__ == "__main__":
    main()
