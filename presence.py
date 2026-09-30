"""Turn Frigate object updates into one greeting per nearby encounter."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Track:
    last_seen: float
    area: int = 0
    name: str = ""
    score: float = 0.0


@dataclass
class PresenceGate:
    target_name: str
    camera_name: str = "desk_camera"
    min_face_score: float = 0.90
    min_person_area: int = 40000
    absence_seconds: float = 25.0
    cooldown_seconds: float = 90.0
    tracks: dict[str, Track] = field(default_factory=dict)
    last_greeting: float = float("-inf")
    last_target_departure: float = float("-inf")
    greeted_encounter: bool = False

    def observe(self, topic: str, payload: dict, now: float | None = None) -> bool:
        """Return True only when a newly nearby, recognized target should be greeted."""
        now = time.monotonic() if now is None else now
        self._expire(now)

        if topic == "frigate/events":
            event = payload.get("after")
            if not isinstance(event, dict):
                return False
            if event.get("camera") != self.camera_name or event.get("label") != "person":
                return False
            object_id = event.get("id")
            if not isinstance(object_id, str) or not object_id:
                return False
            if payload.get("type") == "end" or event.get("end_time") is not None:
                ended = self.tracks.pop(object_id, None)
                if (
                    ended is not None
                    and ended.name == self.target_name
                    and ended.score >= self.min_face_score
                    and not self._target_active()
                ):
                    self.last_target_departure = now
                return False
            track = self.tracks.setdefault(object_id, Track(last_seen=now))
            track.last_seen = now
            track.area = self._area(event)
            sub_label = event.get("sub_label")
            if isinstance(sub_label, (list, tuple)) and len(sub_label) >= 2:
                name, score = sub_label[:2]
                self._set_identity(track, name, score)
            return self._should_greet(track, now)

        if topic == "frigate/tracked_object_update" and payload.get("type") == "face":
            if payload.get("camera") != self.camera_name:
                return False
            object_id = payload.get("id")
            if not isinstance(object_id, str) or object_id not in self.tracks:
                return False
            track = self.tracks[object_id]
            track.last_seen = now
            self._set_identity(track, payload.get("name"), payload.get("score"))
            return self._should_greet(track, now)
        return False

    @staticmethod
    def _area(event: dict) -> int:
        box = event.get("box")
        if isinstance(box, (list, tuple)) and len(box) == 4:
            try:
                return max(0, int(box[2] - box[0])) * max(0, int(box[3] - box[1]))
            except (TypeError, ValueError):
                pass
        try:
            return max(0, int(event.get("area") or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _set_identity(track: Track, name: object, score: object) -> None:
        if not isinstance(name, str):
            return
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            return
        if confidence >= track.score:
            track.name = name
            track.score = confidence

    def _expire(self, now: float) -> None:
        # Frigate does not publish continuous heartbeat events for a stationary
        # object; retain active tracks until an explicit end event arrives.
        self.tracks = {
            object_id: track
            for object_id, track in self.tracks.items()
            if now - track.last_seen < 3600
        }
        if not self._target_active() and now - self.last_target_departure >= self.absence_seconds:
            self.greeted_encounter = False

    def _target_active(self) -> bool:
        return any(
            track.name == self.target_name and track.score >= self.min_face_score
            for track in self.tracks.values()
        )

    def _should_greet(self, track: Track, now: float) -> bool:
        if track.name != self.target_name or track.score < self.min_face_score:
            return False
        if track.area < self.min_person_area:
            return False
        if self.greeted_encounter or now - self.last_greeting < self.cooldown_seconds:
            return False
        self.greeted_encounter = True
        self.last_greeting = now
        return True
