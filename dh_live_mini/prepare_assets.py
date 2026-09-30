"""Generate mini2.0 browser assets after its matching DINet checkpoint is present."""

import gzip
import hashlib
import json
import pickle
import sys
from pathlib import Path

import cv2
from data_preparation_web import data_preparation_web


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: prepare_assets.py OUTPUT_DIRECTORY")
    root = Path(sys.argv[1])
    checkpoint = Path("checkpoint/DINet_mini/epoch_40_new.pth")
    if not checkpoint.is_file():
        raise SystemExit(f"mini2.0 requires {checkpoint}")
    for name in ("processed.mp4", "processed.pkl"):
        if not (root / "data" / name).is_file():
            raise SystemExit(f"missing preprocessed video: {root / 'data' / name}")

    data_preparation_web(str(root))
    assets = root / "assets"
    with gzip.open(assets / "combined_data.json.gz", "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    with (root / "data" / "processed.pkl").open("rb") as stream:
        frame_count = len(pickle.load(stream))
    video = cv2.VideoCapture(str(assets / "01.mp4"))
    video_frame_count = int(video.get(cv2.CAP_PROP_FRAME_COUNT))
    video.release()
    if data.get("size") != 184 or data.get("frame_num") != frame_count:
        raise RuntimeError("unexpected mini2.0 asset dimensions or frame count")
    if video_frame_count != frame_count * 2:
        raise RuntimeError(f"loop video needs {frame_count * 2} forward/reverse frames, got {video_frame_count}")
    version = hashlib.sha256((assets / "01.mp4").read_bytes() +
                             (assets / "combined_data.json.gz").read_bytes()).hexdigest()[:12]
    (assets / "avatar_config.js").write_text(
        "CONFIG.chromaKeyEnabled = false;\n"
        f"CONFIG.videoSrc = 'assets/01.mp4?v={version}';\n"
        f"CONFIG.dataSrc = 'assets/combined_data.json.gz?v={version}';\n"
    )
    print(json.dumps({"status": "ok", "model_size": data["size"],
                      "landmark_frames": data["frame_num"],
                      "video_frames": video_frame_count, "assets": str(assets)}), flush=True)


if __name__ == "__main__":
    main()
