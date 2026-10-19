"""Local "Hey Specter" wake word: short-utterance segmenter, on-device Whisper, phrase match.

中文：本地唤醒词 "Hey Specter"：切出短句 → 本机 Whisper 转写 → 匹配开头的称呼。
Audio never leaves the computer and is not stored. Only short utterances are
transcribed, and nothing is logged unless the phrase matches. Listening pauses
while a conversation runs or another app is dictating.
声音不出本机、不保存；只转写短句，匹配上才记录。对话进行中或别的程序在听写时暂停。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

LOG = logging.getLogger("specter")
SAMPLE_RATE = 16_000
FRAME_SAMPLES = 480  # 30 ms

# EN: Non-native pronunciations and Whisper spellings of the name; "Hi" works like "Hey".
# 中文：容忍口音和 Whisper 的不同拼法；"Hi" 与 "Hey" 等价。
GREETING = r"(?:hey|hi|hay|high|hei|heh|ay|eh|hiya|hello|okay|ok)"
NAME = r"(?:spect(?:er|re|or|ar|a|ah|ra|ur)s?|spektor|spektr|spekter)"


class WakePhraseMatcher:
    def __init__(self, require_greeting: bool = True) -> None:
        greeting = GREETING if require_greeting else f"(?:{GREETING})?"
        self._pattern = re.compile(rf"^\s*{greeting}\s+{NAME}\b(.*)$" if require_greeting
                                   else rf"^\s*(?:{greeting}\s+)?{NAME}\b(.*)$")

    @staticmethod
    def normalize(text: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9']+", " ", text.lower()).split())

    def match(self, text: str) -> str | None:
        """Return what followed the wake phrase ("" if nothing), or None when absent."""
        found = self._pattern.match(self.normalize(text))
        return found.group(1).strip() if found else None


class UtteranceSegmenter:
    """Energy-based segmentation of 30 ms int16 frames into short utterances.

    中文：按音量切句：比底噪明显响即开始，静音 0.6 秒结束；最长只取前 6 秒（称呼在开头）。
    """

    def __init__(self, start_factor: float = 3.0, min_rms: float = 300.0,
                 end_silence_ms: int = 600, min_ms: int = 300, max_ms: int = 6000,
                 pre_roll_ms: int = 300) -> None:
        self.start_factor, self.min_rms = start_factor, min_rms
        self.end_frames = end_silence_ms // 30
        self.min_frames, self.max_frames = min_ms // 30, max_ms // 30
        self.pre_roll: deque[np.ndarray] = deque(maxlen=pre_roll_ms // 30)
        self.noise = 200.0
        self.frames: list[np.ndarray] = []
        self.silent = 0
        self.loud = 0  # loud frames only; pre-roll and pauses do not count as speech
        self.overflowed = False

    def reset(self) -> None:
        self.frames, self.silent, self.loud, self.overflowed = [], 0, 0, False
        self.pre_roll.clear()

    def feed(self, frame: np.ndarray) -> np.ndarray | None:
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        loud = rms > max(self.min_rms, self.noise * self.start_factor)
        if not self.frames and not self.overflowed:
            if loud:
                self.frames = [*self.pre_roll, frame]
                self.silent, self.loud = 0, 1
                return None
            self.noise = 0.95 * self.noise + 0.05 * rms
            self.pre_roll.append(frame)
            return None
        if self.overflowed:
            # Ignore the rest of a long stretch of talk until it pauses.
            self.silent = 0 if loud else self.silent + 1
            if self.silent >= self.end_frames:
                self.overflowed, self.silent = False, 0
            return None
        self.frames.append(frame)
        self.silent = 0 if loud else self.silent + 1
        self.loud += loud
        if len(self.frames) >= self.max_frames:
            segment = np.concatenate(self.frames)
            self.frames, self.loud, self.overflowed = [], 0, True
            return segment
        if self.silent >= self.end_frames:
            # EN: A keyboard click or cough is too short to be "Hey Specter".
            # 中文：敲键盘、咳嗽这类太短的声音不可能是 "Hey Specter"。
            segment = np.concatenate(self.frames) if self.loud >= self.min_frames else None
            self.frames, self.silent, self.loud = [], 0, 0
            return segment
        return None


class WakeListener:
    """Background thread: microphone → segments → local Whisper → on_wake(remainder)."""

    def __init__(self, on_wake: Callable[[str], None], should_listen: Callable[[], bool],
                 device: int | None = None) -> None:
        self.on_wake, self.should_listen, self.device = on_wake, should_listen, device
        self.matcher = WakePhraseMatcher(
            os.getenv("SPECTER_WAKE_REQUIRE_GREETING", "true").strip().lower() in {"1", "true", "yes", "on"},
        )
        self.segmenter = UtteranceSegmenter(
            start_factor=float(os.getenv("SPECTER_WAKE_START_FACTOR", "3.0")),
            min_rms=float(os.getenv("SPECTER_WAKE_MIN_RMS", "300")),
        )
        self.debug = os.getenv("SPECTER_WAKE_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}
        self.model = None

    def load(self) -> None:
        from faster_whisper import WhisperModel

        size = os.getenv("SPECTER_WAKE_MODEL", "base.en")
        started = time.monotonic()
        self.model = WhisperModel(size, device="cpu", compute_type="int8",
                                  cpu_threads=int(os.getenv("SPECTER_WAKE_CPU_THREADS", "4")))
        LOG.info("Wake word model %s ready in %.1f s", size, time.monotonic() - started)

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            audio.astype(np.float32) / 32768.0, language="en", beam_size=1,
            without_timestamps=True, condition_on_previous_text=False, vad_filter=False,
            # EN: Mention the name without the phrase itself: an identical prompt made
            # Whisper drop "Hey Specter" from "Hey Specter, how was your day?".
            # 中文：只提名字、不提整句；提示与开头完全相同时，Whisper 会把称呼当成已说过而漏掉。
            initial_prompt="Specter is a name.",
        )
        return " ".join(segment.text for segment in segments).strip()

    def check(self, audio: np.ndarray) -> str | None:
        return self.matcher.match(self.transcribe(audio))

    def run(self) -> None:
        import sounddevice as sd

        self.load()
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=FRAME_SAMPLES, device=self.device) as stream:
            LOG.info("Wake word listening for \"Hey Specter\"")
            while True:
                frame, _overflow = stream.read(FRAME_SAMPLES)
                if not self.should_listen():
                    self.segmenter.reset()
                    continue
                segment = self.segmenter.feed(frame[:, 0].copy())
                if segment is None:
                    continue
                started = time.monotonic()
                remainder = self.check(segment)
                if self.debug and remainder is None:
                    # Lengths and levels only, never what was said.
                    LOG.info("Wake check: %.1f s utterance, no wake phrase (noise floor %.0f)",
                             len(segment) / SAMPLE_RATE, self.segmenter.noise)
                if remainder is not None and self.should_listen():
                    LOG.info("Wake word heard (%.0f ms to recognize%s)", (time.monotonic() - started) * 1000,
                             ", with a request" if remainder else "")
                    self.on_wake(remainder)
                    self.segmenter.reset()

    def start(self) -> threading.Thread:
        def guarded() -> None:
            try:
                self.run()
            except Exception:
                LOG.exception("Wake word listener stopped")

        thread = threading.Thread(target=guarded, name="wake-word", daemon=True)
        thread.start()
        return thread
