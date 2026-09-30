"""Low-frequency local cat detection on overlapping Frigate frame crops.

Frigate's 300-pixel detector may miss a small cat in a 1280x720 wide shot.
Run the same bundled OpenVINO model on a small set of larger crops instead.
Frigate 广角画面缩到 300 像素后可能漏掉暗处的小猫；本模块复用同一模型，
只在本机对重叠区域做低频扫描，不上传视频，也不保存扫描画面。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from cat_alert import CatCandidate


class TiledCatDetector:
    """Return the highest-confidence cat bounding box in frame pixels."""

    def __init__(self, model_path: Path, min_score: float = 0.55, min_area: int = 8_000):
        import openvino as ov

        self.model = ov.Core().compile_model(str(model_path), "CPU")
        self.output = self.model.output(0)
        self.min_score = min_score
        self.min_area = min_area

    @staticmethod
    def _tiles(width: int, height: int):
        """Scan full view plus nine overlapping crops, including sofa scale.

        中文：整帧保留大目标；九个重叠裁剪让角落里的猫占够模型输入面积。
        """
        yield (0, 0, width, height)
        tile_w = max(300, round(width * 0.40))
        tile_h = max(300, round(height * 0.55))
        for y in (0, (height - tile_h) // 2, height - tile_h):
            for x in (0, (width - tile_w) // 2, width - tile_w):
                yield (x, y, x + tile_w, y + tile_h)

    def detect(self, frame) -> tuple[CatCandidate, float] | None:
        import numpy as np
        from PIL import Image

        best: tuple[CatCandidate, float] | None = None
        for left, top, right, bottom in self._tiles(frame.width, frame.height):
            crop = frame.crop((left, top, right, bottom)).resize(
                (300, 300), Image.Resampling.BILINEAR,
            )
            # EN: Frigate configured this SSD model for uint8 NHWC BGR.
            # 中文：与 Frigate 配置一致，输入是 uint8、NHWC 排列和 BGR 通道。
            rgb = np.asarray(crop, dtype=np.uint8)
            bgr = rgb[:, :, ::-1].copy()[None, ...]
            rows = self.model([bgr])[self.output][0, 0]
            for row in rows:
                if int(row[1]) != 17:  # COCO-91 class 17 is cat / 猫。
                    continue
                score = float(row[2])
                if score < self.min_score or (best is not None and score <= best[1]):
                    continue
                x1 = max(0, min(frame.width, round(left + float(row[3]) * (right - left))))
                y1 = max(0, min(frame.height, round(top + float(row[4]) * (bottom - top))))
                x2 = max(0, min(frame.width, round(left + float(row[5]) * (right - left))))
                y2 = max(0, min(frame.height, round(top + float(row[6]) * (bottom - top))))
                if (x2 - x1) * (y2 - y1) >= self.min_area:
                    best = (CatCandidate("local-cat-scan", (x1, y1, x2, y2)), score)
        return best


@dataclass
class TiledCatArrivalGate:
    """Announce a new arrival only after a confirmed no-cat interval.

    中文：启动时如果猫已在画面里，将它作为基线；它离开再回来才提醒。
    """

    absence_seconds: float = 30.0
    cooldown_seconds: float = 300.0
    initialized: bool = False
    present: bool = False
    present_streak: int = 0
    absent_since: float | None = None
    last_alert: float = float("-inf")
    announced_encounter: bool = False

    def observe(self, candidate: CatCandidate | None, now: float | None = None) -> CatCandidate | None:
        now = time.monotonic() if now is None else now
        if not self.initialized:
            self.initialized = True
            self.present = candidate is not None
            self.present_streak = 1 if candidate else 0
            self.absent_since = None if candidate else now
            return None
        if candidate is None:
            if self.present or self.absent_since is None:
                self.absent_since = now
            self.present = False
            self.present_streak = 0
            if now - self.absent_since >= self.absence_seconds:
                self.announced_encounter = False
            return None
        if self.present:
            self.present_streak += 1
            return None
        self.present_streak += 1
        if self.present_streak < 2:
            return None
        self.present = True
        if self.announced_encounter or now - self.last_alert < self.cooldown_seconds:
            return None
        if self.absent_since is None or now - self.absent_since < self.absence_seconds:
            return None
        return candidate

    def mark_alert(self, now: float | None = None) -> None:
        self.last_alert = time.monotonic() if now is None else now
        self.announced_encounter = True
