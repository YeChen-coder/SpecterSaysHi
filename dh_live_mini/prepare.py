"""Extract video and face landmarks for the pinned mini2.0 asset builder."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from data_preparation_mini import data_preparation_mini


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: prepare.py INPUT_VIDEO OUTPUT_DIRECTORY")
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    if not source.is_file():
        raise SystemExit(f"missing input video: {source}")
    for model in (
        Path("checkpoint/scrfd_2.5g_kps.onnx"),
        Path("checkpoint/face_landmarker_256x256.pt"),
    ):
        if not model.is_file():
            raise SystemExit(f"missing checkpoint: {model}")
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    # Upstream browser reverses only the landmark data. The MP4 must contain
    # the matching reverse frames (including their encoded frame-index pixels).
    result = data_preparation_mini(str(source), str(output), resize_option=True,
                                   reverse_option=True)
    video = output / "data" / "processed.mp4"
    trimmed = output / "data" / "processed_trimmed.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", str(video),
        "-map", "0:v:0", "-frames:v", str(result["frame_count"] * 2),
        "-c", "copy", "-movflags", "+faststart", str(trimmed),
    ], check=True)
    os.replace(trimmed, video)
    for name in ("processed.mp4", "processed.pkl"):
        if not (output / "data" / name).is_file():
            raise RuntimeError(f"preparation did not create {name}")
    print(json.dumps({"status": "ok", "frames": result["frame_count"],
                      "elapsed_seconds": round(time.monotonic() - started, 1),
                      "data": str(output / "data")}), flush=True)


if __name__ == "__main__":
    main()
