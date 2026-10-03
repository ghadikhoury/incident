"""Build a narrated video from real Step 9 dashboard captures.

Requires Pillow, ffmpeg/ffprobe, and Windows PowerShell with System.Speech.
The committed screenshots contain no credentials; they show resolved demo data.
"""

import json
import subprocess
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

HERE = Path(__file__).resolve().parent
BUILD = HERE / ".build"
OUTPUT = HERE / "incident-stage1-demo.mp4"
SIZE = (1920, 1080)
BG = "#0f141b"
CARD = "#171d26"
INK = "#f1f5fb"
MUTED = "#aab8c8"
ACCENT = "#58a6ff"
GREEN = "#4cce73"
FONT = Path("C:/Windows/Fonts/segoeui.ttf")
BOLD = Path("C:/Windows/Fonts/segoeuib.ttf")

SLIDES = [
    {
        "title": "Incident: one view of a cascading failure",
        "points": ["Stage 1 demo", "Real AWS run in us-east-2", "October 3, 2026"],
        "voice": "Incident is a small incident response system for a simulated online shop. "
        "Four services run on EC2. When one fails, CloudWatch detects the symptoms, "
        "the pipeline groups related alarms, and the dashboard shows engineers the evidence. "
        "This is a recorded walkthrough using a real, resolved demo incident.",
    },
    {
        "title": "From alarms to a live incident",
        "points": [
            "CloudWatch alarm",
            "EventBridge to Lambda",
            "DynamoDB, S3, SQS",
            "FastAPI to WebSocket",
        ],
        "voice": "Each service emits metrics and logs to CloudWatch. An alarm transition goes "
        "through EventBridge to Lambda. Lambda writes one correlated incident to "
        "DynamoDB and saves time bounded evidence in S3. An SQS update wakes the backend, "
        "which pushes the change to connected dashboards over WebSocket.",
    },
    {
        "title": "The system before the failure",
        "points": [
            "All four services healthy",
            "Dependencies are visible",
            "Dashboard updates live",
        ],
        "image": ("dashboard.png", None),
        "voice": "Here is the actual dashboard after the test recovered. The gateway depends "
        "on orders. Orders depend on payment and inventory; payment uses Postgres. "
        "The dependency view matters because errors at the gateway may be downstream "
        "symptoms of a payment failure. The simulation panel can inject a controlled fault.",
    },
    {
        "title": "Slow payment database: one incident",
        "points": ["Detected in 85.2 seconds", "7 alarms grouped", "Probable root: payment"],
        "image": ("incident-1012.png", (0, 0, 1440, 625)),
        "voice": "In this run, payment database queries slowed until its connection pool "
        "filled. CloudWatch detected the failure in eighty five point two seconds. "
        "Seven payment, order, and gateway alarms were attached to one incident. "
        "The rule based analysis identified payment as the probable root and showed "
        "the affected downstream services.",
    },
    {
        "title": "Evidence stays attached to the incident",
        "points": ["CloudWatch metric graphs", "Saved log excerpts", "Searchable incident window"],
        "image": ("incident-1012.png", (0, 345, 1440, 1160)),
        "voice": "The incident detail page keeps the metric graphs, saved log excerpts, "
        "related alerts, and timeline together. In this example, the saved payment "
        "logs show PoolTimeout and connection pool exhaustion. Evidence is captured "
        "around the alarm and stored in a private encrypted S3 bucket for later review.",
    },
    {
        "title": "Observed facts and AI are separated",
        "points": ["Facts are rule based", "AI advice needs approval", "This run: AI unavailable"],
        "image": ("incident-1012.png", (0, 1160, 1440, 1800)),
        "voice": "The dashboard labels observed facts separately from AI inference. Bedrock can "
        "suggest a cause and a recovery action, but an engineer must approve before "
        "the action runs. During these measured tests, the account exhausted its "
        "daily Bedrock token quota, so AI analysis was unavailable. We make no claim "
        "about AI diagnostic accuracy from these runs.",
    },
    {
        "title": "What the evaluation showed",
        "points": [
            "4 of 4 probable roots correct",
            "Median detection: 130.3 seconds",
            "Every selected case recovered",
            "AI accuracy: not scored",
        ],
        "voice": "The four selected live failures were slow database, intermittent errors, "
        "CPU pressure, and a process crash. The probable root service was correct "
        "in all four. Median detection was one hundred thirty point three seconds, "
        "and each test recovered. This is a small demo sample, not a production "
        "reliability estimate. Stage one is ready for review, while account quota "
        "and infrastructure migration remain operational gates.",
    },
]


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(BOLD if bold else FONT), size)


def slide_image(index: int, slide: dict) -> Path:
    canvas = Image.new("RGB", SIZE, BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((78, 48), "INCIDENT  /  STAGE 1", font=font(27, True), fill=ACCENT)
    draw.text((78, 100), slide["title"], font=font(64, True), fill=INK)
    draw.rounded_rectangle((66, 208, 1854, 995), radius=24, fill=CARD)
    for line, point in enumerate(slide["points"]):
        y = 284 + line * 118
        draw.ellipse((106, y + 13, 120, y + 27), fill=GREEN)
        draw.text((144, y), point, font=font(35, True), fill=INK)
    if "image" in slide:
        filename, crop = slide["image"]
        source = Image.open(HERE / "assets" / filename).convert("RGB")
        if crop:
            source = source.crop(crop)
        fitted = ImageOps.contain(source, (1080, 690), Image.Resampling.LANCZOS)
        x = 1840 - fitted.width
        y = 254 + (690 - fitted.height) // 2
        canvas.paste(fitted, (x, y))
        draw.rounded_rectangle(
            (x - 3, y - 3, x + fitted.width + 3, y + fitted.height + 3),
            radius=8,
            outline="#66798d",
            width=3,
        )
    elif index == 1:
        draw.text((890, 330), "Alarm -> Incident -> Evidence", font=font(42, True), fill=ACCENT)
        draw.text((890, 424), "CloudWatch -> EventBridge -> Lambda", font=font(32), fill=INK)
        draw.text((890, 504), "DynamoDB + S3 -> SQS -> Dashboard", font=font(32), fill=INK)
    elif index == 0:
        draw.text((840, 348), "DETECT", font=font(61, True), fill=ACCENT)
        draw.text((840, 468), "UNDERSTAND", font=font(61, True), fill=INK)
        draw.text((840, 588), "RECOVER", font=font(61, True), fill=GREEN)
    else:
        draw.text((890, 345), "Evidence-backed incident response", font=font(40, True), fill=ACCENT)
        draw.text((890, 455), "Human approval before recovery", font=font(35), fill=INK)
    draw.text(
        (78, 1023),
        "Real demo captures  •  Incident INC-1012  •  2026-10-03",
        font=font(24),
        fill=MUTED,
    )
    draw.text((1772, 1023), f"{index + 1} / {len(SLIDES)}", font=font(24), fill=MUTED)
    path = BUILD / f"slide-{index:02d}.png"
    canvas.save(path)
    return path


def duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "narration.json").write_text(
        json.dumps([slide["voice"] for slide in SLIDES]), encoding="utf-8"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-File", str(HERE / "narrate.ps1"), "-BuildDir", str(BUILD)],
        check=True,
    )
    clips = []
    for index, slide in enumerate(SLIDES):
        image = slide_image(index, slide)
        voice = BUILD / f"voice-{index:02d}.wav"
        clip = BUILD / f"clip-{index:02d}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-loop",
                "1",
                "-framerate",
                "24",
                "-i",
                str(image),
                "-i",
                str(voice),
                "-af",
                "apad=pad_dur=2",
                "-t",
                str(duration(voice) + 2),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "25",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "24",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(clip),
            ],
            check=True,
        )
        clips.append(clip)
    manifest = BUILD / "clips.txt"
    manifest.write_text("\n".join(f"file '{clip.as_posix()}'" for clip in clips), encoding="utf-8")
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(OUTPUT),
        ],
        check=True,
    )
    total_seconds = sum(duration(BUILD / f"voice-{i:02d}.wav") + 2 for i in range(len(SLIDES)))
    print(f"{OUTPUT} ({total_seconds:.1f}s)")


if __name__ == "__main__":
    main()
