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


@dataclass
class OwnerPresence:
    """Continuous "owner is near the desk camera" state for the situation engine.

    中文：为情境引擎维护"本人是否在桌前"的连续状态，与一次性问候判定分开。
    Frigate re-sends an update at least once a minute for a tracked object,
    including a stationary one, so a silent track goes stale rather than
    staying present forever. A new unnamed track that appears right after the
    owner's track ended inherits the identity (Frigate often re-tracks a seated
    person whose face is turned away); a confident other name cancels that.

    Frigate's ``stationary`` flag and ``position_changes`` counter tell when the
    person's box last moved. A seated typist can stay "stationary" for minutes,
    so camera motion (``frigate/<camera>/motion`` ON/OFF) also counts as moving. A long-motionless track is either someone asleep in view
    or a "ghost" (a chair that kept the name), so the engine treats it with care.
    Optional Frigate zones limit presence to the desk area.
    中文：记录本人最后一次移动的时间。长时间静止的轨迹可能是在镜头里睡着，也可能是
    继承了名字的椅子等"幽灵"目标，由引擎谨慎处理。可选的 Frigate 区域把在场限定在桌前。
    """

    target_name: str
    camera_name: str = "desk_camera"
    min_face_score: float = 0.90
    min_person_area: int = 40000
    stale_seconds: float = 180.0
    inherit_seconds: float = 60.0
    zones: frozenset[str] = frozenset()
    tracks: dict[str, Track] = field(default_factory=dict)
    moved_at: dict[str, float] = field(default_factory=dict)
    position_changes: dict[str, int] = field(default_factory=dict)
    in_zone: dict[str, bool] = field(default_factory=dict)
    motion_active: bool = False
    motion_stopped_at: float = float("-inf")
    inherited: set[str] = field(default_factory=set)
    last_owner_end: float = float("-inf")

    def observe(self, topic: str, payload: dict, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._expire(now)
        if topic == "frigate/events":
            event = payload.get("after")
            if not isinstance(event, dict):
                return
            if event.get("camera") != self.camera_name or event.get("label") != "person":
                return
            object_id = event.get("id")
            if not isinstance(object_id, str) or not object_id:
                return
            if payload.get("type") == "end" or event.get("end_time") is not None:
                self._drop(object_id, now)
                return
            if object_id not in self.tracks and self._may_inherit(now):
                self.inherited.add(object_id)
            track = self.tracks.setdefault(object_id, Track(last_seen=now))
            track.last_seen = now
            track.area = PresenceGate._area(event)
            self._note_motion(object_id, event, now)
            zones = event.get("current_zones")
            self.in_zone[object_id] = not self.zones or (
                isinstance(zones, list) and bool(self.zones.intersection(zones))
            )
            sub_label = event.get("sub_label")
            if isinstance(sub_label, (list, tuple)) and len(sub_label) >= 2:
                PresenceGate._set_identity(track, *sub_label[:2])
        elif topic == "frigate/tracked_object_update" and payload.get("type") == "face":
            object_id = payload.get("id")
            if payload.get("camera") != self.camera_name or object_id not in self.tracks:
                return
            track = self.tracks[object_id]
            track.last_seen = now
            PresenceGate._set_identity(track, payload.get("name"), payload.get("score"))

    def observe_motion(self, state: str, now: float | None = None) -> None:
        """Frigate publishes plain ON/OFF when camera motion starts and ends."""
        now = time.monotonic() if now is None else now
        state = state.strip().upper()
        if state == "ON":
            self.motion_active = True
        elif state == "OFF" and self.motion_active:
            self.motion_active = False
            self.motion_stopped_at = now

    def present(self, now: float | None = None) -> bool:
        return bool(self._present_ids(now))

    def still_seconds(self, now: float | None = None) -> float | None:
        """Seconds since the most recently moving present owner track moved."""
        now = time.monotonic() if now is None else now
        ids = self._present_ids(now)
        if not ids:
            return None
        if self.motion_active:
            return 0.0
        last_moved = max(self.moved_at.get(object_id, now) for object_id in ids)
        return max(0.0, now - max(last_moved, self.motion_stopped_at))

    def _present_ids(self, now: float | None) -> list[str]:
        now = time.monotonic() if now is None else now
        self._expire(now)
        return [
            object_id for object_id, track in self.tracks.items()
            if self._is_owner(object_id) and track.area >= self.min_person_area
            and self.in_zone.get(object_id, not self.zones)
        ]

    def _note_motion(self, object_id: str, event: dict, now: float) -> None:
        changes = event.get("position_changes")
        changes = changes if isinstance(changes, int) and not isinstance(changes, bool) else None
        previous = self.position_changes.get(object_id)
        if (
            object_id not in self.moved_at
            or event.get("stationary") is False
            or (changes is not None and previous is not None and changes > previous)
        ):
            self.moved_at[object_id] = now
        if changes is not None:
            self.position_changes[object_id] = changes

    def _is_owner(self, object_id: str) -> bool:
        track = self.tracks.get(object_id)
        if track is None:
            return False
        if track.score >= self.min_face_score:
            return track.name == self.target_name
        return object_id in self.inherited

    def _may_inherit(self, now: float) -> bool:
        owner_active = any(self._is_owner(object_id) for object_id in self.tracks)
        return not owner_active and now - self.last_owner_end <= self.inherit_seconds

    def _drop(self, object_id: str, now: float) -> None:
        was_owner = self._is_owner(object_id)
        self.tracks.pop(object_id, None)
        self.inherited.discard(object_id)
        self.moved_at.pop(object_id, None)
        self.position_changes.pop(object_id, None)
        self.in_zone.pop(object_id, None)
        if was_owner and not any(self._is_owner(other) for other in self.tracks):
            self.last_owner_end = now

    def _expire(self, now: float) -> None:
        stale = [(k, t.last_seen) for k, t in self.tracks.items() if now - t.last_seen > self.stale_seconds]
        for object_id, last_seen in stale:
            # EN: A silent track ended when it was last seen, not when noticed.
            # 中文：失联的轨迹按最后一次出现时间结束，而不是按发现过期的时间。
            self._drop(object_id, last_seen)
