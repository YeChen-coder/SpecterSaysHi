"""Strict bedtime ladder: coax, warn, lock, harden; promises are checked, not trusted."""

from dataclasses import replace
from datetime import datetime, timedelta

from situation import (
    BEDTIME, TOPIC_GUARD, TOPIC_NUDGE_RESULT, TOPIC_PHONE, TOPIC_PRESENCE, TOPIC_QUIET,
    SituationEngine, SituationSettings,
)

SUNDAY_LATE = datetime(2026, 10, 18, 23, 50)  # bedtime 00:30 (Sunday night)


class Night:
    """Drive the engine in 15 s ticks; reply to each nudge with a scripted answer."""

    def __init__(self, replies=None, settings=None):
        self.engine = SituationEngine(settings or replace(SituationSettings(), bedtime_strict=True))
        self.mono = 50_000.0
        self.local = SUNDAY_LATE
        self.phone = True
        self.label = "X"
        self.replies = dict(replies or {})  # level -> (outcome, detail, snooze_minutes)
        self.nudges = []

    def run(self, minutes):
        for _ in range(int(minutes * 4)):
            self.mono += 15
            self.local += timedelta(seconds=15)
            e = self.engine
            e.observe(TOPIC_PRESENCE, {"present": False}, self.mono)
            e.observe(TOPIC_PHONE, {
                "screen_interactive": self.phone, "device_locked": False,
                "foreground_package": "com.twitter.android", "foreground_label": self.label,
            }, self.mono)
            for decision in e.evaluate(self.mono, self.local):
                if decision.kind != "nudge":
                    continue
                nudge = decision.nudge
                self.nudges.append((self.local.strftime("%H:%M"), nudge))
                assert e.record_outcome(nudge.id, "delivered", self.mono, await_reply=False)
                reply = self.replies.pop(nudge.level, None)
                if reply:
                    outcome, detail, snooze = reply
                    assert e.observe(TOPIC_NUDGE_RESULT, {
                        "id": nudge.id, "outcome": outcome, "detail": detail, "snooze_minutes": snooze,
                    }, self.mono)
        return self

    def bounce(self, times=1):
        for _ in range(times):
            self.engine.observe(TOPIC_GUARD, {"event": "bounce", "package": "tv.danmaku.bili"}, self.mono)

    def ladder(self):
        return [(t, n.level) for t, n in self.nudges]


def test_ignoring_everything_climbs_to_lockdown():
    night = Night().run(65)
    assert night.ladder() == [("00:32", 1), ("00:47", 2), ("00:52", 3)]
    lock = night.nudges[-1][1]
    assert lock.context["guard"] == "block"
    assert lock.context["app_label"] == "X"
    assert lock.context["strict"] is True


def test_bounces_harden_the_lock():
    night = Night().run(65)
    night.bounce(2)
    night.run(1)
    assert night.ladder()[-1][1] == 3
    night.bounce(1)
    night.run(0.25)
    assert night.ladder()[-1][1] == 4
    assert night.nudges[-1][1].context["guard"] == "block_lock"
    night.bounce(5)
    night.run(60)
    assert len(night.nudges) == 4  # nothing beyond level 4


def test_bounces_before_lockdown_do_not_count():
    night = Night().run(10)
    night.bounce(5)
    assert night.engine.bounces == 0


def test_fighting_the_lock_for_half_an_hour_hardens_it():
    night = Night().run(65)
    night.run(31)
    assert night.ladder()[-1] == ("01:22", 4)


def test_saying_going_to_sleep_and_staying_on_is_caught():
    night = Night(replies={1: ("accepted", "", None)}).run(50)
    assert night.ladder() == [("00:32", 1), ("00:35", 3)]
    assert night.nudges[-1][1].context["caught"] == "accept"


def test_keeping_the_promise_then_relapsing_gets_a_warning():
    night = Night(replies={1: ("accepted", "", None)}).run(42)  # 00:32 L1
    night.phone = False
    night.run(4)  # screen off through the 3-minute check at 00:35
    assert night.ladder() == [("00:32", 1)]
    night.run(10)
    night.phone = True
    night.run(25)
    # The clock restarted at the 00:35 check: warning at 00:50, then the lock.
    assert night.ladder() == [("00:32", 1), ("00:50", 2), ("00:55", 3)]


def test_one_snooze_then_lock_without_asking():
    night = Night(replies={1: ("snoozed", "", 10)}).run(52)
    assert night.ladder() == [("00:32", 1), ("00:42", 3)]
    assert night.nudges[-1][1].context["snooze_over"] is True


def test_stay_with_me_gets_a_longer_grace():
    night = Night(replies={1: ("accepted", "stay", None)}).run(52)
    assert night.ladder() == [("00:32", 1), ("00:38", 3)]
    assert night.nudges[-1][1].context["caught"] == "stay"


def test_snooze_at_warning_is_not_granted():
    night = Night(replies={2: ("snoozed", "", 10)}).run(70)
    assert night.ladder()[:3] == [("00:32", 1), ("00:47", 2), ("00:52", 3)]


def test_daily_cap_does_not_stop_the_ladder_but_quiet_does():
    capped = Night(settings=replace(SituationSettings(), bedtime_strict=True, daily_max=1)).run(65)
    assert [level for _, level in capped.ladder()] == [1, 2, 3]

    released = Night()
    until = (SUNDAY_LATE + timedelta(hours=7)).timestamp() * 1000
    released.engine.observe(TOPIC_QUIET, {"until_ms": until}, released.mono)
    released.run(60)
    assert released.nudges == []


def test_asleep_before_the_first_nudge_means_no_ladder():
    night = Night()
    night.phone = False
    night.run(90)
    assert night.nudges == []
    night.phone = True
    night.run(30)
    assert night.nudges == []


def test_next_night_starts_fresh():
    night = Night().run(65)
    night.bounce(3)
    night.run(1)
    assert night.ladder()[-1][1] == 4
    night.local = datetime(2026, 10, 20, 0, 0)  # Monday night, bedtime 00:30
    night.run(40)
    assert night.ladder()[-1] == ("00:32", 1)
    assert (night.engine.bed_stage, night.engine.bounces, night.engine.snooze_used) == (1, 0, False)
