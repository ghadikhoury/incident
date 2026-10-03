# Stage 1 demo video

[Watch the 2:32 video](incident-stage1-demo.mp4). The dashboard captures come
from the running EC2 demo and resolved incident INC-1012. No credentials or
private keys are in the images or video. The narration reports the actual
Step 9 measurements and labels the Bedrock quota limitation.

To rebuild on Windows, install Pillow 12, ensure ffmpeg is on `PATH`, and run
`python docs/demo/build_demo.py` from the repository root. The builder uses
Windows System.Speech for narration, writes temporary frames to the ignored
`docs/demo/.build/` directory, and replaces the MP4.
