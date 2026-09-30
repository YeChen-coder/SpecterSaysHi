"""One-shot cat arrival alert, separate from the human Realtime conversation.

Frigate or a local tiled scan detects a cat; an Agents SDK vision check compares
the live crop with both cats' private reference photos. A conflicting Frigate
classification falls back to a generic cat alert.
Frigate 或本机局部扫描发现候选猫后，视觉 Agent 比对两只猫的私有参考图；
分类结果冲突时用通用提醒，不建立持续监听或多轮对话。
"""

from __future__ import annotations

import base64
import io
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agents import Agent, RunConfig, Runner
from openai import OpenAI
from pydantic import BaseModel

from realtime_agent import audio_device
from visual import FrigateFrameSource, VisualUnavailable

LOG = logging.getLogger("specter")
CatIdentity = Literal["tabo", "chichi", "cat", "none"]
NAMED_CATS = {"tabo", "chichi"}


@dataclass(frozen=True)
class CatCandidate:
    """A recent Frigate cat event and its bounding box in detect-frame pixels."""

    object_id: str
    box: tuple[int, int, int, int]


@dataclass
class _CatTrack:
    attempts: int = 0
    last_attempt: float = float("-inf")
    pending: bool = False
    classifier_label: str = ""
    classifier_score: float = 0.0
    classifier_ready: threading.Event = field(default_factory=threading.Event)


@dataclass
class CatArrivalGate:
    """Deduplicate object updates and allow one alert per cat encounter.

    中文：同一只猫的重复事件只尝试有限次数；离开足够久后才允许下次播报。
    """

    camera_name: str
    min_area: int = 8_000
    min_score: float = 0.70
    absence_seconds: float = 30.0
    cooldown_seconds: float = 300.0
    retry_seconds: float = 3.0
    max_attempts: int = 3
    classifier_model: str = "household_cats"
    tracks: dict[str, _CatTrack] = field(default_factory=dict)
    last_departure: float = float("-inf")
    last_alert: float = float("-inf")
    alerted_encounter: bool = False

    def observe_classification(self, payload: dict) -> None:
        """Keep the classifier result for an active Frigate cat track only."""
        if (payload.get("type") != "classification"
                or payload.get("camera") != self.camera_name
                or payload.get("model") != self.classifier_model):
            return
        track = self.tracks.get(payload.get("id"))
        if track is None:
            return
        label = payload.get("sub_label")
        if not isinstance(label, str) or label.lower() not in NAMED_CATS:
            return
        try:
            score = float(payload.get("score"))
        except (TypeError, ValueError):
            return
        if not 0.8 <= score <= 1.0:
            return
        track.classifier_label = label.lower()
        track.classifier_score = score
        track.classifier_ready.set()

    def classifier_result(self, object_id: str) -> tuple[str, float] | None:
        track = self.tracks.get(object_id)
        if track is None or not track.classifier_ready.is_set():
            return None
        return track.classifier_label, track.classifier_score

    def classifier_event(self, object_id: str) -> threading.Event | None:
        track = self.tracks.get(object_id)
        return track.classifier_ready if track is not None else None

    def observe(self, payload: dict, now: float | None = None) -> CatCandidate | None:
        """Return a candidate only for a live, sufficiently large cat box."""
        now = time.monotonic() if now is None else now
        event = payload.get("after")
        if not isinstance(event, dict) or event.get("camera") != self.camera_name:
            return None
        if event.get("label") != "cat":
            return None
        object_id = event.get("id")
        if not isinstance(object_id, str) or not object_id:
            return None

        if payload.get("type") == "end" or event.get("end_time") is not None:
            self.tracks.pop(object_id, None)
            if not self.tracks:
                self.last_departure = now
            return None

        # EN: Frigate may reuse a new track ID after a brief occlusion. Only
        # reset the encounter after all cats have been absent long enough.
        # 中文：短暂遮挡会产生新 ID；要等所有猫离开足够久才重置提醒状态。
        if not self.tracks and now - self.last_departure >= self.absence_seconds:
            self.alerted_encounter = False
        track = self.tracks.setdefault(object_id, _CatTrack())
        if self.alerted_encounter or now - self.last_alert < self.cooldown_seconds:
            return None
        if track.pending or track.attempts >= self.max_attempts:
            return None
        if now - track.last_attempt < self.retry_seconds:
            return None
        try:
            score = float(event.get("top_score") or event.get("score") or 0)
            box = tuple(int(value) for value in event["box"])
        except (KeyError, TypeError, ValueError):
            return None
        if len(box) != 4 or score < self.min_score:
            return None
        if (box[2] - box[0]) * (box[3] - box[1]) < self.min_area:
            return None
        track.attempts += 1
        track.last_attempt = now
        track.pending = True
        return CatCandidate(object_id, box)

    def complete(self, object_id: str, announced: bool, now: float | None = None) -> None:
        """Close an attempt; suppress repeats only after successful playback."""
        now = time.monotonic() if now is None else now
        track = self.tracks.get(object_id)
        if track is not None:
            track.pending = False
        if announced:
            self.alerted_encounter = True
            self.last_alert = now


class CatVerdict(BaseModel):
    """Structured identity decision; uncertainty never triggers audio."""

    match: Literal["tabo", "chichi", "other", "uncertain"]
    cat_visible: bool
    clear_view: bool
    reason: str


class CatIdentifier:
    """Compare a fresh Frigate cat crop with the owner's reference images.

    中文：照片只在内存中转为数据 URL，供单次视觉判断；不保存摄像头截图。
    """

    def __init__(self, source: FrigateFrameSource, reference_dir: Path, model: str,
                 chichi_reference_dir: Path):
        self.source = source
        self.model = model
        paths = sorted(reference_dir.glob("*.jpg"))
        if len(paths) < 2:
            raise ValueError(f"Need at least two Tabo reference JPGs in {reference_dir}")
        self.tabo_references = [self._image_url(path.read_bytes(), 640) for path in paths[:6]]
        chichi_paths = sorted(chichi_reference_dir.glob("*.jpg"))
        if not chichi_paths:
            raise ValueError(f"Need a chichi reference JPG in {chichi_reference_dir}")
        self.chichi_references = [self._image_url(path.read_bytes(), 640) for path in chichi_paths[:6]]

    @staticmethod
    def _image_url(raw: bytes, max_side: int) -> str:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    def _live_crop(self, box: tuple[int, int, int, int], frame=None) -> bytes:
        """Crop around Frigate's box while keeping surrounding fur visible."""
        frame = frame if frame is not None else self.source.latest_frame()
        x1, y1, x2, y2 = box
        margin_x = max(8, int((x2 - x1) * 0.12))
        margin_y = max(8, int((y2 - y1) * 0.12))
        bounds = (
            max(0, x1 - margin_x), max(0, y1 - margin_y),
            min(frame.width, x2 + margin_x), min(frame.height, y2 + margin_y),
        )
        if bounds[2] - bounds[0] < 80 or bounds[3] - bounds[1] < 80:
            raise VisualUnavailable("Cat crop is too small or outside the camera frame")
        output = io.BytesIO()
        crop = frame.crop(bounds)
        crop.thumbnail((768, 768))
        crop.save(output, format="JPEG", quality=88, optimize=True)
        return output.getvalue()

    def classify(self, candidate: CatCandidate, frame=None) -> CatIdentity:
        """Use one stateless Agent run to separate cat presence from identity.

        中文：只有清楚认出 Tabo 或 chichi 才报名字；无法确定身份时只报猫。
        """
        live_url = self._image_url(self._live_crop(candidate.box, frame), 768)
        content = [{
            "type": "input_text",
            "text": (
                "参考照分别标注 Tabo 与 chichi；最后一张‘待识别’图片来自摄像头，"
                "也可能只是沙发、阴影或其他物品。先独立判断最后一张是否确实有猫。"
                "Tabo 是有白色鼻口与胸口的虎斑猫，chichi 是黑猫；"
                "请比较毛色、脸部、身体轮廓及可见斑纹。"
                "只有身份特征清晰且与对应参考照一致时输出 tabo 或 chichi；"
                "明确不是这两只时输出 other；看不到足够特征时输出 uncertain。"
            ),
        }]
        for index, url in enumerate(self.tabo_references, 1):
            content.append({"type": "input_text", "text": f"Tabo 参考照 {index}"})
            content.append({"type": "input_image", "image_url": url, "detail": "high"})
        for index, url in enumerate(self.chichi_references, 1):
            content.append({"type": "input_text", "text": f"chichi 参考照 {index}"})
            content.append({"type": "input_image", "image_url": url, "detail": "high"})
        content.append({"type": "input_text", "text": "待识别的现场画面："})
        content.append({"type": "input_image", "image_url": live_url, "detail": "high"})
        agent = Agent(
            name="Cat identity checker",
            model=self.model,
            output_type=CatVerdict,
            instructions=(
                "Compare the final live cat image with both cats' labeled reference images. "
                "Tabo is a tabby with white muzzle and chest; chichi is black. "
                "Return uncertain if the live cat is too small, partly outside the crop, "
                "or distinctive markings cannot be compared. If distinctive markings "
                "match, return tabo or chichi; otherwise return other. "
                "Set cat_visible independently of whether identity "
                "can be determined; false positives must not cause announcements. "
                "Give one brief reason. Do not identify people."
            ),
        )
        result = Runner.run_sync(
            agent, [{"role": "user", "content": content}], max_turns=1,
            run_config=RunConfig(tracing_disabled=True),
        )
        verdict = result.final_output
        # EN: Log only the decision, never a description of private images.
        # 中文：日志只留身份结论，不记录家庭画面的文字描述。
        LOG.info("Cat visual check: %s (cat=%s, identity_clear=%s)",
                 verdict.match, verdict.cat_visible, verdict.clear_view)
        if verdict.cat_visible and verdict.match in NAMED_CATS and verdict.clear_view:
            return verdict.match
        return "cat" if verdict.cat_visible else "none"

    def is_tabo(self, candidate: CatCandidate) -> bool:
        """Compatibility helper for a simple positive-reference diagnostic."""
        return self.classify(candidate) == "tabo"


def resolve_cat_identity(vision: CatIdentity,
                         classifier: tuple[str, float] | None) -> CatIdentity:
    """A known conflicting Frigate identity must never produce a named alert."""
    if vision not in NAMED_CATS or classifier is None:
        return vision
    return vision if classifier[0] == vision else "cat"


class OneShotCatAnnouncer:
    """Speak one fixed sentence, then close the audio stream immediately.

    中文：TTS 只播放一句话；不打开麦克风，也不启动 Realtime Session。
    """

    def __init__(self, audio_active: threading.Event):
        self.audio_active = audio_active
        self._pcm: dict[str, bytes] = {}
        self._prepare_lock = threading.Lock()

    def prepare(self, kind: Literal["tabo", "chichi", "cat"] = "tabo") -> bytes:
        """Generate the fixed sentence once, in memory, before it is needed.

        中文：提前准备短句，猫进镜头时无需再等待语音合成；不会产生音频文件。
        """
        with self._prepare_lock:
            if kind not in self._pcm:
                client = OpenAI(timeout=20.0)
                response = client.audio.speech.create(
                    model=os.getenv("SPECTER_CAT_TTS_MODEL", "gpt-4o-mini-tts"),
                    voice=os.getenv("SPECTER_CAT_TTS_VOICE", "sage"),
                    input={"tabo": "Tabo is here.", "chichi": "Chichi is here.",
                           "cat": "A cat is here."}[kind],
                    instructions="Speak in calm, restrained English with a mature, low-energy delivery. Say only the given sentence.",
                    response_format="pcm",
                )
                self._pcm[kind] = response.content
            return self._pcm[kind]

    def speak(self, kind: Literal["tabo", "chichi", "cat"] = "tabo") -> None:
        import sounddevice as sd

        pcm = self.prepare(kind)
        device = audio_device(sd, os.getenv("SPECTER_SPEAKER_NAME", ""), "output")
        self.audio_active.set()
        try:
            with sd.RawOutputStream(samplerate=24_000, channels=1, dtype="int16", device=device) as stream:
                stream.write(pcm)
            # EN: Keep the Realtime microphone muted through the room's echo tail.
            # 中文：播放结束后稍等回声消失，再恢复实时会话的麦克风上行。
            time.sleep(0.25)
        finally:
            self.audio_active.clear()
        LOG.info("Played one-shot %s arrival alert", kind)
