"""Fuse desk presence, FOCI and phone state into rare, budgeted nudges.

中文：融合桌前在场、FOCI 与手机状态，在打扰预算内产生主动提醒（催睡觉、久坐）。
The engine is pure logic. The host feeds it MQTT payloads plus a monotonic
clock and local wall time, then decides how each returned decision is delivered.
引擎不连接 MQTT、不读系统时钟；宿主负责喂入消息与时间，并决定如何投递提醒。
"""

from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock, timedelta
from typing import Any

TOPIC_PRESENCE = "specter/presence/owner"
TOPIC_FOCI = "specter/foci/state"
TOPIC_PHONE = "specter/phone/status"
TOPIC_SESSION = "specter/session/state"
TOPIC_QUIET = "specter/situation/quiet"
TOPIC_NUDGE_REQUEST = "specter/nudge/request"
TOPIC_NUDGE_RESULT = "specter/nudge/result"
TOPIC_GUARD = "specter/phone/guard"
INPUT_TOPICS = (
    TOPIC_PRESENCE, TOPIC_FOCI, TOPIC_PHONE, TOPIC_SESSION, TOPIC_QUIET, TOPIC_NUDGE_RESULT,
    TOPIC_GUARD,
)

BEDTIME = "bedtime"
SEDENTARY = "sedentary"
NEGATIVE_OUTCOMES = frozenset({"declined", "ignored"})
ENGAGED_OUTCOMES = frozenset({"accepted", "snoozed"})
FINAL_OUTCOMES = NEGATIVE_OUTCOMES | ENGAGED_OUTCOMES
UNDELIVERED_OUTCOMES = frozenset({"skipped_busy", "failed"})


def _env_number(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite, non-negative number")
    return value


def _env_clock(name: str, default: str) -> clock:
    text = os.getenv(name, default).strip()
    try:
        hour, minute = (int(part) for part in text.split(":"))
        return clock(hour, minute)
    except ValueError as exc:
        raise ValueError(f"{name} must be HH:MM, for example {default}") from exc


@dataclass(frozen=True)
class SituationSettings:
    """Durations are seconds; field defaults match config.example.env."""

    bedtime: clock = clock(0, 30)
    weekend_bedtime: clock = clock(1, 30)
    morning: clock = clock(6, 0)
    bedtime_awake_seconds: float = 120
    bedtime_repeat_seconds: float = 20 * 60
    bedtime_max_nudges: int = 3
    asleep_seconds: float = 15 * 60
    sit_seconds: float = 50 * 60
    sit_break_seconds: float = 5 * 60
    sit_repeat_seconds: float = 20 * 60
    sit_max_nudges: int = 2
    flow_defer_seconds: float = 20 * 60
    flow_states: frozenset[str] = frozenset({"flow", "focused"})
    min_interval_seconds: float = 20 * 60
    daily_max: int = 8
    backoff_after: int = 2
    presence_stale_seconds: float = 180
    awake_still_seconds: float = 10 * 60
    ghost_still_seconds: float = 30 * 60
    phone_stale_seconds: float = 45
    foci_recent_seconds: float = 240
    pending_timeout_seconds: float = 120
    reply_window_seconds: float = 15 * 60
    compliance_seconds: float = 60
    # EN: Strict bedtime: coax, warn, then lock entertainment apps until morning.
    # 中文：管教型催睡：先哄、再警告、然后封锁娱乐应用到早上。
    bedtime_strict: bool = False
    warn_after_seconds: float = 15 * 60
    lock_after_warning_seconds: float = 5 * 60
    promise_grace_seconds: float = 3 * 60
    stay_grace_seconds: float = 6 * 60
    harden_bounces: int = 3
    harden_after_lock_seconds: float = 30 * 60

    @classmethod
    def from_env(cls) -> SituationSettings:
        minutes = lambda name, default: _env_number(name, default) * 60  # noqa: E731
        flow = frozenset(
            part.strip().lower()
            for part in os.getenv("SPECTER_SIT_FLOW_STATES", "flow,focused").split(",")
            if part.strip()
        )
        settings = cls(
            bedtime=_env_clock("SPECTER_BEDTIME", "00:30"),
            weekend_bedtime=_env_clock("SPECTER_BEDTIME_WEEKEND", "01:30"),
            morning=_env_clock("SPECTER_MORNING", "06:00"),
            bedtime_awake_seconds=_env_number("SPECTER_BEDTIME_AWAKE_SECONDS", 120),
            bedtime_repeat_seconds=minutes("SPECTER_BEDTIME_REPEAT_MINUTES", 20),
            bedtime_max_nudges=int(_env_number("SPECTER_BEDTIME_MAX_NUDGES", 3)),
            asleep_seconds=minutes("SPECTER_ASLEEP_MINUTES", 15),
            sit_seconds=minutes("SPECTER_SIT_MINUTES", 50),
            sit_break_seconds=minutes("SPECTER_SIT_BREAK_MINUTES", 5),
            sit_repeat_seconds=minutes("SPECTER_SIT_REPEAT_MINUTES", 20),
            sit_max_nudges=int(_env_number("SPECTER_SIT_MAX_NUDGES", 2)),
            flow_defer_seconds=minutes("SPECTER_SIT_FLOW_DEFER_MINUTES", 20),
            flow_states=flow,
            min_interval_seconds=minutes("SPECTER_NUDGE_MIN_INTERVAL_MINUTES", 20),
            daily_max=int(_env_number("SPECTER_NUDGE_DAILY_MAX", 8)),
            backoff_after=int(_env_number("SPECTER_NUDGE_BACKOFF_AFTER", 2)),
            presence_stale_seconds=_env_number("SPECTER_PRESENCE_STALE_SECONDS", 180),
            awake_still_seconds=minutes("SPECTER_AWAKE_STILL_MINUTES", 10),
            ghost_still_seconds=minutes("SPECTER_PRESENCE_GHOST_MINUTES", 30),
            foci_recent_seconds=_env_number("SPECTER_SITUATION_FOCI_RECENT_SECONDS", 240),
            reply_window_seconds=minutes("SPECTER_NUDGE_REPLY_MINUTES", 15),
            bedtime_strict=os.getenv("SPECTER_BEDTIME_STRICT", "true").strip().lower() in {
                "1", "true", "yes", "on",
            },
            warn_after_seconds=minutes("SPECTER_BEDTIME_WARN_AFTER_MINUTES", 15),
            lock_after_warning_seconds=minutes("SPECTER_BEDTIME_LOCK_AFTER_WARNING_MINUTES", 5),
            promise_grace_seconds=minutes("SPECTER_BEDTIME_PROMISE_GRACE_MINUTES", 3),
            stay_grace_seconds=minutes("SPECTER_BEDTIME_STAY_GRACE_MINUTES", 6),
            harden_bounces=int(_env_number("SPECTER_BEDTIME_HARDEN_BOUNCES", 3)),
            harden_after_lock_seconds=minutes("SPECTER_BEDTIME_HARDEN_AFTER_MINUTES", 30),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        noon = clock(12, 0)
        if not self.morning < noon:
            raise ValueError("SPECTER_MORNING must be before 12:00")
        for bedtime in (self.bedtime, self.weekend_bedtime):
            # EN: An after-midnight bedtime must still come before morning.
            # 中文：凌晨的就寝时间必须早于早晨结束时间。
            if bedtime < noon and not bedtime < self.morning:
                raise ValueError("Bedtimes after midnight must be earlier than SPECTER_MORNING")
        if (self.sit_seconds <= 0 or self.sit_break_seconds <= 0
                or self.presence_stale_seconds <= 0 or self.backoff_after < 1):
            raise ValueError("Invalid SPECTER_SIT_* / SPECTER_NUDGE_* values in config.local.env")

    def night_window(self, local: datetime) -> tuple[date, datetime, datetime]:
        """Return the night's evening date and its bedtime-to-morning window.

        中文：一个夜晚归属于它开始的那天傍晚；中午作为两晚的分界。
        Friday and Saturday evenings use the weekend bedtime.
        """
        night = (local - timedelta(hours=12)).date()
        bedtime = self.weekend_bedtime if night.weekday() in (4, 5) else self.bedtime
        start_day = night if bedtime >= clock(12, 0) else night + timedelta(days=1)
        start = datetime.combine(start_day, bedtime, local.tzinfo)
        end = datetime.combine(night + timedelta(days=1), self.morning, local.tzinfo)
        return night, start, end

    def day_key(self, local: datetime) -> date:
        """The nudge budget resets at morning, not at midnight."""
        return (local - timedelta(hours=self.morning.hour, minutes=self.morning.minute)).date()


@dataclass(frozen=True)
class Nudge:
    id: str
    rule: str
    level: int
    channel: str
    context: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id, "rule": self.rule, "level": self.level,
            "channel": self.channel, "context": dict(self.context),
        }


@dataclass(frozen=True)
class Decision:
    """kind is nudge (deliver now), held (due but blocked), or info (state note)."""

    kind: str
    rule: str
    reason: str
    nudge: Nudge | None = None


@dataclass
class _Signals:
    owner_present: bool = False
    presence_at: float | None = None
    presence_still: float | None = None
    foci_label: str = ""
    foci_label_at: float | None = None
    phone_screen_on: bool = False
    phone_app: str = ""
    phone_label: str = ""
    phone_at: float | None = None
    session_kind: str | None = None
    quiet_until: float = 0.0


@dataclass
class _Candidate:
    rule: str
    level: int
    channel: str
    context: dict[str, Any]
    hold: str = ""


@dataclass
class SituationEngine:
    settings: SituationSettings = field(default_factory=SituationSettings.from_env)
    signals: _Signals = field(default_factory=_Signals)
    # Sitting session at the desk. 中文：桌前连续久坐状态。
    sit_start: float | None = None
    absent_since: float | None = None
    sit_nudges: int = 0
    sit_last: float | None = None
    sit_deferred_until: float | None = None
    # Current night. 中文：当晚的催睡状态。
    night: date | None = None
    bed_nudges: int = 0
    bed_last: float | None = None
    awake_since: float | None = None
    asleep_since: float | None = None
    night_closed: bool = False
    ghost_noted: bool = False
    # Strict ladder state for the current night. 中文：当晚管教阶梯的状态。
    bed_stage: int = 0
    bed_stage_at: float | None = None
    promise_until: float | None = None
    promise_kind: str = ""
    broken_promise: str = ""
    snooze_used: bool = False
    bounces: int = 0
    delivered_levels: dict[str, int] = field(default_factory=dict)
    # Interruption budget. 中文：打扰预算。
    day: date | None = None
    day_count: int = 0
    last_delivered: float | None = None
    negatives: dict[str, int] = field(default_factory=dict)
    snoozed_until: dict[str, float] = field(default_factory=dict)
    pending: dict[str, tuple[Nudge, float]] = field(default_factory=dict)
    delivered: dict[str, str] = field(default_factory=dict)
    # EN: Delivered nudges still waiting for an answer: id -> [rule, delivered_at, compliant_since].
    # 中文：已送达、仍在等回应的提醒：编号 -> [规则, 送达时间, 开始照做的时间]。
    awaiting: dict[str, list] = field(default_factory=dict)
    last_hold: dict[str, str] = field(default_factory=dict)

    # ----- inputs -----

    def observe(self, topic: str, payload: Any, now: float) -> bool:
        """Apply one MQTT payload; return False when it was not usable."""
        if not isinstance(payload, dict):
            return False
        s = self.signals
        if topic == TOPIC_PRESENCE:
            present, still = payload.get("present"), payload.get("still_seconds")
            if not isinstance(present, bool):
                return False
            s.owner_present, s.presence_at = present, now
            s.presence_still = (
                float(still) if isinstance(still, (int, float)) and not isinstance(still, bool)
                and math.isfinite(still) and still >= 0 else None
            )
        elif topic == TOPIC_FOCI:
            # EN: The dashboard pauses its stream in cycles; keep the last real
            # label and judge recency instead of trusting `connected` alone.
            # 中文：FOCI 仪表盘会间歇暂停推送；保留最后一条真实标签并按时间判断新旧。
            label = payload.get("state_label")
            if payload.get("connected") and isinstance(label, str) and label:
                s.foci_label, s.foci_label_at = label.lower(), now
        elif topic == TOPIC_PHONE:
            interactive, locked = payload.get("screen_interactive"), payload.get("device_locked")
            if not isinstance(interactive, bool) or not isinstance(locked, bool):
                return False
            # EN: Use the raw screen fields: full-screen video can leave the
            # usage signal at `unknown` while the person is clearly awake.
            # 中文：直接看屏幕字段；全屏看视频时使用信号可能是 unknown，但人显然醒着。
            s.phone_screen_on, s.phone_at = interactive and not locked, now
            app = payload.get("foreground_package")
            s.phone_app = app if isinstance(app, str) else ""
            label = payload.get("foreground_label")
            s.phone_label = label[:60] if isinstance(label, str) else ""
        elif topic == TOPIC_SESSION:
            active = payload.get("active")
            if active is not None and not isinstance(active, str):
                return False
            s.session_kind = active or None
        elif topic == TOPIC_QUIET:
            until_ms = payload.get("until_ms", 0)
            if not isinstance(until_ms, (int, float)) or isinstance(until_ms, bool):
                return False
            s.quiet_until = max(0.0, float(until_ms) / 1000)
        elif topic == TOPIC_NUDGE_RESULT:
            nudge_id, outcome = payload.get("id"), payload.get("outcome")
            if not isinstance(nudge_id, str) or not isinstance(outcome, str):
                return False
            snooze = payload.get("snooze_minutes")
            snooze_seconds = float(snooze) * 60 if isinstance(snooze, (int, float)) else None
            await_reply = payload.get("await_reply", True) is not False
            detail = payload.get("detail")
            return self.record_outcome(
                nudge_id, outcome, now, snooze_seconds, await_reply,
                detail if isinstance(detail, str) else "",
            )
        elif topic == TOPIC_GUARD:
            # EN: Each app the phone bounced during a lockdown counts as resistance.
            # 中文：封锁期间每被弹回一次，都算一次"不听话"。
            if payload.get("event") != "bounce":
                return False
            if self.bed_stage >= 3:
                self.bounces += 1
        else:
            return False
        return True

    def record_outcome(
        self, nudge_id: str, outcome: str, now: float, snooze_seconds: float | None = None,
        await_reply: bool = True, detail: str = "",
    ) -> bool:
        """delivered counts against the budget; later outcomes tune backoff.

        await_reply=False (silent logging) means no answer can ever arrive, so
        no outcome is inferred and backoff stays untouched.
        中文：静默记录不会有回应，因此不推断结果、不影响退避。
        """
        if outcome == "delivered":
            entry = self.pending.pop(nudge_id, None)
            if entry is None:
                return False
            nudge = entry[0]
            self.delivered[nudge_id] = nudge.rule
            if await_reply:
                self.awaiting[nudge_id] = [nudge.rule, now, None]
            self.snoozed_until.pop(nudge.rule, None)
            self.day_count += 1
            self.last_delivered = now
            if nudge.rule == BEDTIME:
                self.bed_nudges += 1
                self.bed_last = now
                self.delivered_levels[nudge_id] = nudge.level
                if self.settings.bedtime_strict:
                    self.bed_stage, self.bed_stage_at = max(self.bed_stage, nudge.level), now
                    self.promise_until, self.promise_kind = None, ""
                    if nudge.level >= 3:
                        self.broken_promise = ""
                    if nudge.level == 3:
                        self.bounces = 0
            else:
                self.sit_nudges += 1
                self.sit_last = now
            return True
        if outcome in UNDELIVERED_OUTCOMES:
            return self.pending.pop(nudge_id, None) is not None
        if outcome not in FINAL_OUTCOMES:
            return False
        if nudge_id in self.pending:
            # EN: A final answer can overtake the "delivered" report; it implies delivery.
            # 中文：最终回应可能早于"已送达"到达；有回应即说明已经送达。
            self.record_outcome(nudge_id, "delivered", now)
        self.awaiting.pop(nudge_id, None)
        rule = self.delivered.pop(nudge_id, None)
        if rule is None:
            return False
        level = self.delivered_levels.pop(nudge_id, 0)
        if rule == BEDTIME and self.settings.bedtime_strict:
            self._strict_reply(level, outcome, detail, now, snooze_seconds)
            return True
        if outcome in NEGATIVE_OUTCOMES:
            self.negatives[rule] = self.negatives.get(rule, 0) + 1
        else:
            self.negatives[rule] = 0
        if outcome == "snoozed" and snooze_seconds:
            self.snoozed_until[rule] = now + max(0.0, snooze_seconds)
        return True

    # ----- evidence -----

    @staticmethod
    def _fresh(at: float | None, now: float, limit: float) -> bool:
        return at is not None and 0 <= now - at <= limit

    def still_seconds(self, now: float) -> float | None:
        """Motionless time of the present owner, extrapolated from the last message."""
        s = self.signals
        if s.presence_still is None or s.presence_at is None:
            return None
        return s.presence_still + max(0.0, now - s.presence_at)

    def _present_recently(self, now: float) -> bool:
        s = self.signals
        return s.owner_present and self._fresh(s.presence_at, now, self.settings.presence_stale_seconds)

    def at_desk(self, now: float) -> bool:
        """Present, unless motionless so long it is likely a ghost or a sleeper."""
        still = self.still_seconds(now)
        return self._present_recently(now) and (still is None or still < self.settings.ghost_still_seconds)

    def awake_at_desk(self, now: float) -> bool:
        # EN: Someone asleep within the camera's view must not count as awake.
        # 中文：在镜头里睡着的人不能算作醒着。
        still = self.still_seconds(now)
        return self.at_desk(now) and (still is None or still < self.settings.awake_still_seconds)

    def phone_awake(self, now: float) -> bool:
        s = self.signals
        return s.phone_screen_on and self._fresh(s.phone_at, now, self.settings.phone_stale_seconds)

    def recent_foci_label(self, now: float) -> str:
        s = self.signals
        if self._fresh(s.foci_label_at, now, self.settings.foci_recent_seconds):
            return s.foci_label
        return ""

    def evidence(self, now: float) -> list[str]:
        found = []
        if self.awake_at_desk(now):
            found.append("desk")
        if self.phone_awake(now):
            found.append("phone")
        if self.recent_foci_label(now) not in {"", "not_worn"}:
            found.append("foci")
        return found

    # ----- evaluation -----

    def evaluate(self, now: float, local: datetime) -> list[Decision]:
        """Advance state and return what to deliver, hold, or note at this tick."""
        decisions: list[Decision] = []
        self._roll_day(local)
        self._expire_pending(now)
        decisions.extend(self._infer_replies(now))
        ghost = self._present_recently(now) and not self.at_desk(now)
        if ghost and not self.ghost_noted:
            decisions.append(Decision("info", "presence", "long_motionless_presence_ignored"))
        self.ghost_noted = ghost
        self._update_sitting(now)
        night, start, end = self.settings.night_window(local)
        in_night = start <= local < end
        if self._update_night(night, in_night, now):
            decisions.append(Decision("info", BEDTIME, "asleep_night_closed"))
        if in_night:
            candidate = self._bedtime_candidate(now, local, start)
            self.last_hold.pop(SEDENTARY, None)
        else:
            candidate = self._sedentary_candidate(now)
            self.last_hold.pop(BEDTIME, None)
        if candidate is None:
            self.last_hold.clear()
            return decisions
        if any(nudge.rule == candidate.rule for nudge, _ in self.pending.values()):
            return decisions
        hold = candidate.hold or self._budget_hold(candidate.rule, now, local)
        if hold:
            if self.last_hold.get(candidate.rule) != hold:
                self.last_hold[candidate.rule] = hold
                decisions.append(Decision("held", candidate.rule, hold))
            return decisions
        self.last_hold.pop(candidate.rule, None)
        nudge = Nudge(
            uuid.uuid4().hex[:12], candidate.rule, candidate.level,
            candidate.channel, candidate.context,
        )
        self.pending[nudge.id] = (nudge, now)
        decisions.append(Decision("nudge", nudge.rule, "due", nudge))
        return decisions

    def _strict_reply(self, level: int, outcome: str, detail: str, now: float,
                      snooze_seconds: float | None) -> None:
        """A promise is only a promise: he checks it when the grace period ends.

        中文：答应只是答应。宽限期一到他就检查，你还在用手机就算说谎，直接封锁。
        """
        if level >= 3:
            return
        s = self.settings
        if outcome == "accepted":
            stay = detail == "stay"
            self.promise_until = now + (s.stay_grace_seconds if stay else s.promise_grace_seconds)
            self.promise_kind = "stay" if stay else "accept"
        elif outcome == "snoozed" and level == 1 and not self.snooze_used:
            # EN: One extra stretch per night; when it ends, no more asking.
            # 中文：每晚只能延后一次；时间到了不再商量。
            self.snooze_used = True
            self.promise_until = now + (snooze_seconds or 600)
            self.promise_kind = "snooze"

    def _busy(self, now: float) -> bool:
        return self.phone_awake(now) or self.awake_at_desk(now)

    def _engaged(self, now: float) -> bool:
        """Awake for a while and actually on the phone or in the living room."""
        return (self.awake_since is not None
                and now - self.awake_since >= self.settings.bedtime_awake_seconds
                and self._busy(now))

    def _strict_candidate(self, now: float, local: datetime, start: datetime) -> _Candidate | None:
        s = self.settings
        stage = self.bed_stage
        if stage >= 4 or (self.night_closed and stage < 3):
            return None
        if self.promise_until is not None and now >= self.promise_until:
            if self._busy(now):
                self.broken_promise = self.promise_kind
            else:
                # Kept it so far; a relapse starts the clock again.
                self.bed_stage_at = now
            self.promise_until, self.promise_kind = None, ""
        extra: dict[str, Any] = {}
        if self.broken_promise and stage < 3:
            level = 3
            if self.broken_promise == "snooze":
                extra["snooze_over"] = True
            else:
                extra["caught"] = self.broken_promise
        elif self.promise_until is not None:
            return None
        elif stage == 0:
            if not self._engaged(now):
                return None
            level = 1
        elif stage in (1, 2):
            wait = s.warn_after_seconds if stage == 1 else s.lock_after_warning_seconds
            if now - self.bed_stage_at < wait or not self._engaged(now):
                return None
            level = stage + 1
        else:
            fighting = (now - self.bed_stage_at >= s.harden_after_lock_seconds and self._engaged(now))
            if self.bounces < s.harden_bounces and not fighting:
                return None
            level = 4
        evidence = self.evidence(now)
        context: dict[str, Any] = {
            "strict": True,
            "minutes_past_bedtime": int((local - start).total_seconds() // 60),
            "evidence": evidence,
            "previous_nudges_tonight": self.bed_nudges,
            **extra,
        }
        if self.phone_awake(now):
            if self.signals.phone_app:
                context["phone_app"] = self.signals.phone_app
            if self.signals.phone_label:
                context["app_label"] = self.signals.phone_label
        if "foci" in evidence:
            context["foci_label"] = self.recent_foci_label(now)
        if level >= 3:
            context["guard"] = "block" if level == 3 else "block_lock"
            context["bounces"] = self.bounces
        channel = "desk" if "desk" in evidence else "phone" if "phone" in evidence else "room"
        return _Candidate(BEDTIME, level, channel, context)

    def _complied(self, rule: str, now: float) -> bool:
        if rule == BEDTIME:
            return not self.phone_awake(now) and not self.awake_at_desk(now)
        return not self.at_desk(now)

    def _infer_replies(self, now: float) -> list[Decision]:
        """Without a tap or spoken answer, judge by what the person did next.

        中文：没有点按钮或口头回应时，按之后的实际行为判断：照做（熄屏/离开）算接受，
        超过回应窗口仍没变化算忽略。
        """
        decisions = []
        for nudge_id, entry in list(self.awaiting.items()):
            rule, delivered_at, compliant_since = entry
            if self._complied(rule, now):
                entry[2] = compliant_since = compliant_since if compliant_since is not None else now
            else:
                entry[2] = compliant_since = None
            if compliant_since is not None and now - compliant_since >= self.settings.compliance_seconds:
                self.record_outcome(nudge_id, "accepted", now)
                decisions.append(Decision("info", rule, "inferred_accepted"))
            elif now - delivered_at >= self.settings.reply_window_seconds:
                self.record_outcome(nudge_id, "ignored", now)
                decisions.append(Decision("info", rule, "inferred_ignored"))
        return decisions

    def _roll_day(self, local: datetime) -> None:
        day = self.settings.day_key(local)
        if day != self.day:
            self.day, self.day_count = day, 0
            self.negatives.clear()

    def _expire_pending(self, now: float) -> None:
        limit = self.settings.pending_timeout_seconds
        for nudge_id in [k for k, (_, at) in self.pending.items() if now - at > limit]:
            del self.pending[nudge_id]

    def _update_sitting(self, now: float) -> None:
        if self.at_desk(now):
            self.absent_since = None
            if self.sit_start is None:
                self.sit_start = now
                self.sit_nudges, self.sit_last, self.sit_deferred_until = 0, None, None
                self.snoozed_until.pop(SEDENTARY, None)
            return
        if self.sit_start is None:
            return
        # EN: Short trips (water, door) do not count as a real break.
        # 中文：短暂离开（倒水、开门）不算真正休息，久坐计时继续。
        if self.absent_since is None:
            self.absent_since = now
        if now - self.absent_since >= self.settings.sit_break_seconds:
            self.sit_start = self.absent_since = None

    def _update_night(self, night: date, in_night: bool, now: float) -> bool:
        """Track awake/asleep evidence; return True when the night just closed."""
        if night != self.night:
            self.night = night
            self.bed_nudges, self.bed_last, self.night_closed = 0, None, False
            self.awake_since = self.asleep_since = None
            self.bed_stage, self.bed_stage_at = 0, None
            self.promise_until, self.promise_kind, self.broken_promise = None, "", ""
            self.snooze_used, self.bounces = False, 0
            self.snoozed_until.pop(BEDTIME, None)
        if not in_night or self.night_closed:
            return False
        if self.evidence(now):
            self.asleep_since = None
            if self.awake_since is None:
                self.awake_since = now
            return False
        self.awake_since = None
        if self.asleep_since is None:
            self.asleep_since = now
        if now - self.asleep_since >= self.settings.asleep_seconds:
            # EN: No desk, phone or FOCI activity for a while: assume asleep and
            # stop nudging until the next night.
            # 中文：一段时间内桌前、手机、FOCI 都没有活动，视为已睡，当晚不再提醒。
            self.night_closed = True
            return True
        return False

    def _repeat_factor(self, rule: str) -> float:
        return 2.0 if self.negatives.get(rule, 0) >= self.settings.backoff_after else 1.0

    def _bedtime_candidate(self, now: float, local: datetime, start: datetime) -> _Candidate | None:
        s = self.settings
        if s.bedtime_strict:
            return self._strict_candidate(now, local, start)
        if self.night_closed or self.bed_nudges >= s.bedtime_max_nudges:
            return None
        if self.awake_since is None or now - self.awake_since < s.bedtime_awake_seconds:
            return None
        due_at = float("-inf")
        if self.bed_last is not None:
            due_at = self.bed_last + s.bedtime_repeat_seconds * self._repeat_factor(BEDTIME)
        if now < self.snoozed_until.get(BEDTIME, due_at):
            return None
        evidence = self.evidence(now)
        context: dict[str, Any] = {
            "minutes_past_bedtime": int((local - start).total_seconds() // 60),
            "evidence": evidence,
            "previous_nudges_tonight": self.bed_nudges,
        }
        if "phone" in evidence and self.signals.phone_app:
            context["phone_app"] = self.signals.phone_app
        if "foci" in evidence:
            context["foci_label"] = self.recent_foci_label(now)
        channel = "desk" if "desk" in evidence else "phone" if "phone" in evidence else "room"
        return _Candidate(BEDTIME, self.bed_nudges + 1, channel, context)

    def _sedentary_candidate(self, now: float) -> _Candidate | None:
        s = self.settings
        if self.sit_start is None or not self.at_desk(now) or self.sit_nudges >= s.sit_max_nudges:
            return None
        if self.sit_last is None:
            due_at = self.sit_start + s.sit_seconds
        else:
            due_at = self.sit_last + s.sit_repeat_seconds * self._repeat_factor(SEDENTARY)
        # EN: An accepted snooze ("ten more minutes") replaces the repeat spacing.
        # 中文：用户要求的延后（"再给我十分钟"）取代默认的重复间隔。
        if now < self.snoozed_until.get(SEDENTARY, due_at):
            return None
        label = self.recent_foci_label(now)
        context: dict[str, Any] = {
            "sitting_minutes": int((now - self.sit_start) // 60),
            "previous_nudges_this_sitting": self.sit_nudges,
        }
        if label:
            context["foci_label"] = label
        candidate = _Candidate(SEDENTARY, self.sit_nudges + 1, "desk", context)
        # EN: Protect deep focus once per sitting, for a bounded time.
        # 中文：每段久坐最多为深度专注让路一次，且有时间上限。
        if self.sit_deferred_until is None and label in s.flow_states and s.flow_defer_seconds > 0:
            self.sit_deferred_until = now + s.flow_defer_seconds
        if self.sit_deferred_until is not None and now < self.sit_deferred_until:
            candidate.hold = "deferred_for_focus"
        return candidate

    def _budget_hold(self, rule: str, now: float, local: datetime) -> str:
        s = self.settings
        if self.signals.session_kind:
            return "session_active"
        if local.timestamp() < self.signals.quiet_until:
            return "quiet"
        # EN: The strict bedtime ladder must be able to finish; it skips the daily cap.
        # 中文：管教阶梯必须能走完，不受每日上限约束。
        if self.day_count >= s.daily_max and not (rule == BEDTIME and s.bedtime_strict):
            return "daily_cap"
        # EN: Bedtime keeps its own repeat spacing, and a snooze the user asked
        # for keeps its promised time; both still count toward the daily cap.
        # 中文：催睡觉有自己的间隔；用户主动要求的延后按约定时间提醒；二者仍计入每日上限。
        if (rule != BEDTIME and rule not in self.snoozed_until
                and self.last_delivered is not None
                and now - self.last_delivered < s.min_interval_seconds):
            return "min_interval"
        return ""
