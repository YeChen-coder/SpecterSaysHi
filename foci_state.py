"""Read the existing FOCI dashboard stream and evaluate sustained adverse states."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger("specter")

def _env_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _env_states(name: str, default: str) -> frozenset[str]:
    states = frozenset(part.strip().lower() for part in os.getenv(name, default).split(",") if part.strip())
    if not states:
        raise ValueError(f"{name} must contain at least one state")
    return states


@dataclass(frozen=True)
class FociSettings:
    adverse_states: frozenset[str]
    recovered_states: frozenset[str]
    future_tolerance_seconds: float
    max_sample_age_seconds: float
    max_sample_gap_seconds: float
    window_seconds: float
    min_samples: int
    min_duration_seconds: float
    min_adverse_fraction: float
    strong_adverse_fraction: float
    max_mean_focus_depth: float
    recovery_seconds: float
    min_valid_focus_depth: int
    max_valid_focus_depth: int

    @classmethod
    def from_env(cls) -> FociSettings:
        settings = cls(
            adverse_states=_env_states("SPECTER_FOCI_ADVERSE_STATES", "distracted,stressed,fatigued"),
            recovered_states=_env_states("SPECTER_FOCI_RECOVERED_STATES", "calm,focused,flow"),
            future_tolerance_seconds=_env_float("SPECTER_FOCI_FUTURE_TOLERANCE_SECONDS", 2),
            max_sample_age_seconds=_env_float("SPECTER_FOCI_MAX_SAMPLE_AGE_SECONDS", 5),
            max_sample_gap_seconds=_env_float("SPECTER_FOCI_MAX_SAMPLE_GAP_SECONDS", 5),
            window_seconds=_env_float("SPECTER_FOCI_WINDOW_SECONDS", 90),
            min_samples=int(os.getenv("SPECTER_FOCI_MIN_SAMPLES", "30")),
            min_duration_seconds=_env_float("SPECTER_FOCI_MIN_DURATION_SECONDS", 60),
            min_adverse_fraction=_env_float("SPECTER_FOCI_MIN_ADVERSE_FRACTION", 0.70),
            strong_adverse_fraction=_env_float("SPECTER_FOCI_STRONG_ADVERSE_FRACTION", 0.85),
            max_mean_focus_depth=_env_float("SPECTER_FOCI_MAX_MEAN_FOCUS_DEPTH", 65),
            recovery_seconds=_env_float("SPECTER_FOCI_RECOVERY_SECONDS", 30),
            min_valid_focus_depth=int(os.getenv("SPECTER_FOCI_MIN_VALID_FOCUS_DEPTH", "0")),
            max_valid_focus_depth=int(os.getenv("SPECTER_FOCI_MAX_VALID_FOCUS_DEPTH", "100")),
        )
        if settings.adverse_states & settings.recovered_states:
            raise ValueError("FOCI adverse and recovered states must not overlap")
        if (settings.future_tolerance_seconds < 0 or settings.max_sample_age_seconds <= 0
                or settings.max_sample_gap_seconds <= 0 or settings.window_seconds <= 0
                or settings.min_samples < 1 or settings.min_duration_seconds <= 0
                or settings.min_duration_seconds > settings.window_seconds
                or not 0 < settings.min_adverse_fraction <= settings.strong_adverse_fraction <= 1
                or settings.recovery_seconds < 0
                or not 0 <= settings.min_valid_focus_depth <= settings.max_valid_focus_depth <= 100
                or not settings.min_valid_focus_depth <= settings.max_mean_focus_depth <= settings.max_valid_focus_depth):
            raise ValueError("Invalid SPECTER_FOCI_* thresholds in config.local.env")
        return settings


@dataclass(frozen=True)
class FociAssessment:
    reason: str
    adverse_fraction: float
    mean_focus_depth: float | None
    duration_seconds: float


class FociStateEvaluator:
    """Conservative first pass; thresholds are product heuristics, not calibration."""

    def __init__(self, settings: FociSettings | None = None) -> None:
        self.settings = settings if settings is not None else FociSettings.from_env()
        self.samples: deque[tuple[float, str, int | None]] = deque()
        self.last_timestamp: float | None = None
        self.healthy_since: float | None = None
        self.latched = False

    def unavailable(self) -> None:
        self.samples.clear()
        self.last_timestamp = None
        self.healthy_since = None

    def observe(self, event: dict[str, Any], *, now: float | None = None) -> FociAssessment | None:
        now = time.time() if now is None else now
        timestamp = event.get("received_at")
        state = event.get("state_label")
        if (
            event.get("kind") != "realtime"
            or not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
            or not math.isfinite(timestamp) or timestamp <= 0
            or timestamp > now + self.settings.future_tolerance_seconds
            or now - timestamp > self.settings.max_sample_age_seconds
            or not isinstance(state, str)
        ):
            self.unavailable()
            return None
        if self.last_timestamp is not None:
            if timestamp <= self.last_timestamp:
                return None
            if timestamp - self.last_timestamp > self.settings.max_sample_gap_seconds:
                self.unavailable()
        self.last_timestamp = timestamp
        if state not in self.settings.adverse_states | self.settings.recovered_states:
            self.samples.clear()
            self.healthy_since = None
            return None
        if self.latched:
            if state in self.settings.recovered_states:
                self.healthy_since = self.healthy_since or timestamp
                if timestamp - self.healthy_since >= self.settings.recovery_seconds:
                    self.latched = False
                    self.samples.clear()
            else:
                self.healthy_since = None
            return None
        depth = event.get("focus_depth")
        depth = depth if (isinstance(depth, int) and not isinstance(depth, bool)
                          and self.settings.min_valid_focus_depth <= depth <= self.settings.max_valid_focus_depth) else None
        self.samples.append((timestamp, state, depth))
        while self.samples and timestamp - self.samples[0][0] > self.settings.window_seconds:
            self.samples.popleft()
        if state not in self.settings.adverse_states or len(self.samples) < self.settings.min_samples:
            return None
        duration = timestamp - self.samples[0][0]
        if duration < self.settings.min_duration_seconds:
            return None
        adverse = [sample for sample in self.samples if sample[1] in self.settings.adverse_states]
        fraction = len(adverse) / len(self.samples)
        if fraction < self.settings.min_adverse_fraction:
            return None
        depths = [sample[2] for sample in adverse if sample[2] is not None]
        mean_depth = sum(depths) / len(depths) if depths else None
        # Focus depth is a weak corroborating factor. Very persistent adverse
        # labels can trigger without it; a low number alone never triggers.
        if (fraction < self.settings.strong_adverse_fraction
                and (mean_depth is None or mean_depth > self.settings.max_mean_focus_depth)):
            return None
        states = {sample[1] for sample in adverse}
        reason = ", ".join(sorted(states))
        return FociAssessment(reason, fraction, mean_depth, duration)

    def acknowledge_trigger(self) -> None:
        self.latched = True
        self.healthy_since = None
        self.samples.clear()


async def watch_foci(
    on_trigger: Callable[[FociAssessment], bool],
    *,
    url: str = "http://127.0.0.1:8765",
    evaluator: FociStateEvaluator | None = None,
) -> None:
    """Subscribe only to the local dashboard; never initiate a BLE connection."""
    import websockets

    evaluator = evaluator or FociStateEvaluator()
    last_failure: str | None = None
    ws_url = url.replace("http", "ws", 1) + "/ws"

    def read_status() -> dict[str, Any]:
        with urllib.request.urlopen(url + "/api/status", timeout=5) as response:
            status = json.load(response)
        return status if isinstance(status, dict) else {}

    while True:
        try:
            status = await asyncio.to_thread(read_status)
            if not status.get("connected"):
                evaluator.unavailable()
                await asyncio.sleep(3)
                continue
            async with websockets.connect(ws_url, open_timeout=5, ping_interval=20) as socket:
                last_failure = None
                LOG.info("FOCI state monitor subscribed to local dashboard")
                while True:
                    message = await asyncio.wait_for(socket.recv(), timeout=10)
                    try:
                        event = json.loads(message)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("kind") == "control":
                        config = event.get("config")
                        if not isinstance(config, dict) or not config.get("connected"):
                            evaluator.unavailable()
                            break
                    elif event.get("kind") == "realtime":
                        assessment = evaluator.observe(event)
                        if assessment is not None:
                            try:
                                accepted = on_trigger(assessment)
                            except Exception:
                                LOG.exception("FOCI trigger dispatch failed")
                            else:
                                if accepted:
                                    evaluator.acknowledge_trigger()
        except (OSError, asyncio.TimeoutError, urllib.error.URLError,
                websockets.exceptions.WebSocketException) as exc:
            failure = type(exc).__name__
            if failure != last_failure:
                LOG.warning("FOCI local data feed unavailable: %s", failure)
            last_failure = failure
        evaluator.unavailable()
        await asyncio.sleep(3)
