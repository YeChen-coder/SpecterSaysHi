import time
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from presence import OwnerPresence
from session_gate import SessionGate
from situation import (
    BEDTIME, SEDENTARY, TOPIC_FOCI, TOPIC_NUDGE_RESULT, TOPIC_PHONE, TOPIC_PRESENCE,
    TOPIC_QUIET, TOPIC_SESSION, SituationEngine, SituationSettings,
)

MONDAY_AFTERNOON = datetime(2026, 10, 19, 14, 0)
MONDAY_LATE = datetime(2026, 10, 19, 23, 50)


class Sim:
    """Drive the engine in 15-second ticks, re-sending the current signals."""

    def __init__(self, start, settings=None, deliver=True, outcome=None, snooze_minutes=None,
                 await_reply=False):
        self.engine = SituationEngine(settings or SituationSettings())
        self.mono = 10_000.0
        self.local = start
        self.desk = False
        self.phone = False
        self.foci = None
        self.deliver = deliver
        self.outcome = outcome
        self.snooze_minutes = snooze_minutes
        self.await_reply = await_reply
        self.nudges = []
        self.decisions = []

    def run(self, minutes, tick=15):
        for _ in range(int(minutes * 60 // tick)):
            self.mono += tick
            self.local += timedelta(seconds=tick)
            engine = self.engine
            engine.observe(TOPIC_PRESENCE, {"present": self.desk}, self.mono)
            engine.observe(TOPIC_PHONE, {
                "screen_interactive": self.phone, "device_locked": False,
                "foreground_package": "tv.danmaku.bili",
            }, self.mono)
            if self.foci:
                engine.observe(TOPIC_FOCI, {"connected": True, "state_label": self.foci}, self.mono)
            for decision in engine.evaluate(self.mono, self.local):
                self.decisions.append((self.local, decision))
                if decision.kind != "nudge":
                    continue
                self.nudges.append((self.local, decision.nudge))
                if self.deliver:
                    assert engine.record_outcome(
                        decision.nudge.id, "delivered", self.mono, await_reply=self.await_reply,
                    )
                    if self.outcome:
                        assert engine.observe(TOPIC_NUDGE_RESULT, {
                            "id": decision.nudge.id, "outcome": self.outcome,
                            "snooze_minutes": self.snooze_minutes,
                        }, self.mono)
        return self

    def times(self):
        return [local.strftime("%H:%M") for local, _ in self.nudges]

    def held(self):
        return [(local.strftime("%H:%M"), d.reason) for local, d in self.decisions if d.kind == "held"]


def test_sedentary_nudge_after_fifty_minutes_at_desk():
    sim = Sim(MONDAY_AFTERNOON)
    sim.desk = True
    sim.run(49.5)
    assert sim.nudges == []
    sim.run(1)
    assert sim.times() == ["14:50"]
    nudge = sim.nudges[0][1]
    assert (nudge.rule, nudge.level, nudge.channel) == (SEDENTARY, 1, "desk")
    assert nudge.context["sitting_minutes"] == 50


def test_short_trip_keeps_sitting_but_real_break_resets_it():
    sim = Sim(MONDAY_AFTERNOON)
    sim.desk = True
    sim.run(30)
    sim.desk = False
    sim.run(3)
    sim.desk = True
    sim.run(18)
    assert sim.times() == ["14:50"]

    rested = Sim(MONDAY_AFTERNOON)
    rested.desk = True
    rested.run(45)
    rested.desk = False
    rested.run(6)
    rested.desk = True
    rested.run(45)
    assert rested.nudges == []


def test_sedentary_repeats_then_stops_at_cap():
    sim = Sim(MONDAY_AFTERNOON)
    sim.desk = True
    sim.run(180)
    assert sim.times() == ["14:50", "15:10"]
    assert [n.level for _, n in sim.nudges] == [1, 2]


def test_repeated_declines_double_the_spacing():
    settings = replace(SituationSettings(), sit_max_nudges=4)
    sim = Sim(MONDAY_AFTERNOON, settings, outcome="declined")
    sim.desk = True
    sim.run(180)
    # 20 min after the first decline, then 40 min once two declines accumulate.
    assert sim.times() == ["14:50", "15:10", "15:50", "16:30"]


def test_snooze_replaces_repeat_spacing():
    sim = Sim(MONDAY_AFTERNOON, outcome="snoozed", snooze_minutes=10)
    sim.desk = True
    sim.run(70)
    assert sim.times() == ["14:50", "15:00"]


def test_deep_focus_defers_once_for_bounded_time():
    sim = Sim(MONDAY_AFTERNOON)
    sim.desk = True
    sim.foci = "flow"
    sim.run(100)
    assert sim.held() == [("14:50", "deferred_for_focus")]
    assert sim.times() == ["15:10", "15:30"]


def test_stale_foci_label_does_not_defer():
    sim = Sim(MONDAY_AFTERNOON)
    sim.desk = True
    sim.foci = "flow"
    sim.run(30)
    sim.foci = None  # dashboard paused or disconnected for > 4 minutes
    sim.run(21)
    assert sim.times() == ["14:50"]


def test_active_session_holds_without_log_spam_then_delivers():
    sim = Sim(MONDAY_AFTERNOON)
    sim.desk = True
    sim.run(49)
    sim.engine.observe(TOPIC_SESSION, {"active": "greeting"}, sim.mono)
    sim.run(6)
    assert sim.nudges == []
    assert sim.held() == [("14:50", "session_active")]
    sim.engine.observe(TOPIC_SESSION, {"active": None}, sim.mono)
    sim.run(1)
    assert sim.times() == ["14:55"]


def test_quiet_until_holds_nudges():
    sim = Sim(MONDAY_AFTERNOON)
    sim.desk = True
    until = (MONDAY_AFTERNOON + timedelta(hours=1)).timestamp() * 1000
    assert sim.engine.observe(TOPIC_QUIET, {"until_ms": until}, sim.mono)
    sim.run(65)
    assert sim.held() == [("14:50", "quiet")]
    assert sim.times() == ["15:00"]


def test_daily_cap_and_minimum_interval():
    capped = Sim(MONDAY_AFTERNOON, replace(SituationSettings(), daily_max=1))
    capped.desk = True
    capped.run(90)
    assert capped.times() == ["14:50"]
    assert capped.held() == [("15:10", "daily_cap")]

    spaced = Sim(MONDAY_AFTERNOON, replace(SituationSettings(), sit_repeat_seconds=300))
    spaced.desk = True
    spaced.run(90)
    assert spaced.times() == ["14:50", "15:10"]
    assert ("14:55", "min_interval") in spaced.held()


def test_bedtime_nudges_from_phone_and_repeats_up_to_cap():
    sim = Sim(MONDAY_LATE)
    sim.phone = True
    sim.run(120)
    assert sim.times() == ["00:32", "00:52", "01:12"]
    first = sim.nudges[0][1]
    assert (first.rule, first.level, first.channel) == (BEDTIME, 1, "phone")
    assert first.context["evidence"] == ["phone"]
    assert first.context["phone_app"] == "tv.danmaku.bili"
    assert first.context["minutes_past_bedtime"] == 2


def test_bedtime_ignores_sedentary_and_global_interval():
    sim = Sim(datetime(2026, 10, 19, 23, 30))
    sim.desk = True
    sim.run(90)
    # Sedentary fires at 00:20 before the window; bedtime may still follow
    # 12 minutes later because it keeps its own repeat spacing.
    assert [(t.strftime("%H:%M"), n.rule) for t, n in sim.nudges] == [
        ("00:20", SEDENTARY), ("00:32", BEDTIME), ("00:52", BEDTIME),
    ]
    assert sim.nudges[1][1].channel == "desk"


def test_sleep_closes_the_night_until_the_next_one():
    sim = Sim(MONDAY_LATE)
    sim.phone = True
    sim.run(50)
    assert sim.times() == ["00:32"]
    sim.phone = False
    sim.run(20)
    infos = [(t.strftime("%H:%M"), d.reason) for t, d in sim.decisions if d.kind == "info"]
    assert infos == [("00:55", "asleep_night_closed")]
    sim.phone = True  # a brief glance at the phone later that night
    sim.run(60)
    assert sim.times() == ["00:32"]
    sim.local = datetime(2026, 10, 21, 0, 0)  # Tuesday night
    sim.run(40)
    assert sim.times()[-1] == "00:32"
    assert len(sim.nudges) == 2


def test_weekend_bedtime_and_window_bounds():
    settings = SituationSettings()
    night, start, end = settings.night_window(datetime(2026, 10, 23, 23, 0))  # Friday
    assert night.isoformat() == "2026-10-23"
    assert start == datetime(2026, 10, 24, 1, 30)
    assert end == datetime(2026, 10, 24, 6, 0)
    night, start, _ = settings.night_window(datetime(2026, 10, 20, 3, 0))  # Monday night
    assert night.isoformat() == "2026-10-19"
    assert start == datetime(2026, 10, 20, 0, 30)
    evening = replace(settings, bedtime=datetime.strptime("23:30", "%H:%M").time())
    _, start, _ = evening.night_window(datetime(2026, 10, 19, 22, 0))
    assert start == datetime(2026, 10, 19, 23, 30)


def test_pending_nudge_is_not_duplicated_and_expires():
    sim = Sim(MONDAY_AFTERNOON, deliver=False)
    sim.desk = True
    sim.run(51)
    assert sim.times() == ["14:50"]
    sim.run(2)
    assert sim.times() == ["14:50", "14:52"]  # undelivered request timed out after 120 s
    nudge_id = sim.nudges[-1][1].id
    assert sim.engine.record_outcome(nudge_id, "skipped_busy", sim.mono)
    sim.run(0.25)
    assert len(sim.nudges) == 3


def test_stale_presence_is_not_at_desk():
    engine = SituationEngine(SituationSettings())
    engine.observe(TOPIC_PRESENCE, {"present": True}, 0)
    assert engine.at_desk(180)
    assert not engine.at_desk(181)


def test_rejects_malformed_payloads():
    engine = SituationEngine(SituationSettings())
    assert not engine.observe(TOPIC_PRESENCE, {"present": "yes"}, 0)
    assert not engine.observe(TOPIC_PHONE, {"screen_interactive": True}, 0)
    assert not engine.observe(TOPIC_SESSION, {"active": 3}, 0)
    assert not engine.observe(TOPIC_NUDGE_RESULT, {"id": "missing", "outcome": "accepted"}, 0)
    assert not engine.observe("other/topic", {}, 0)
    assert not engine.observe(TOPIC_PRESENCE, ["not", "a", "dict"], 0)


@pytest.mark.parametrize("name, value", [
    ("SPECTER_BEDTIME", "25:00"),
    ("SPECTER_BEDTIME", "late"),
    ("SPECTER_BEDTIME", "07:00"),  # after morning
    ("SPECTER_MORNING", "13:00"),
    ("SPECTER_SIT_MINUTES", "0"),
    ("SPECTER_NUDGE_DAILY_MAX", "-1"),
])
def test_invalid_settings_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        SituationSettings.from_env()


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("SPECTER_BEDTIME", "23:45")
    monkeypatch.setenv("SPECTER_SIT_MINUTES", "40")
    monkeypatch.setenv("SPECTER_SIT_FLOW_STATES", "Flow")
    settings = SituationSettings.from_env()
    assert settings.bedtime.strftime("%H:%M") == "23:45"
    assert settings.sit_seconds == 2400
    assert settings.flow_states == frozenset({"flow"})


def face_event(object_id, name=None, area=50000, kind="update", score=0.96):
    side = int(area**0.5)
    return {
        "type": kind,
        "after": {
            "id": object_id, "camera": "desk_camera", "label": "person",
            "box": [0, 0, side, side],
            "sub_label": [name, score] if name else None,
            "end_time": 1 if kind == "end" else None,
        },
    }


def test_owner_presence_requires_identity_and_nearness():
    presence = OwnerPresence("Alice")
    presence.observe("frigate/events", face_event("r", "Bob"), now=0)
    presence.observe("frigate/events", face_event("x"), now=0)
    assert not presence.present(1)
    presence.observe("frigate/events", face_event("a", "Alice", area=10000), now=2)
    assert not presence.present(3)
    presence.observe("frigate/events", face_event("a", "Alice", area=60000), now=4)
    assert presence.present(5)
    presence.observe("frigate/events", face_event("a", "Alice", kind="end"), now=6)
    assert not presence.present(7)


def test_owner_presence_inherits_a_quick_retrack_only():
    presence = OwnerPresence("Alice")
    presence.observe("frigate/events", face_event("a", "Alice"), now=0)
    presence.observe("frigate/events", face_event("a", "Alice", kind="end"), now=10)
    presence.observe("frigate/events", face_event("b"), now=50)
    assert presence.present(51)
    # A confident different face cancels the inherited identity.
    presence.observe("frigate/tracked_object_update", {
        "type": "face", "id": "b", "camera": "desk_camera", "name": "Bob", "score": 0.95,
    }, now=52)
    assert not presence.present(53)

    late = OwnerPresence("Alice")
    late.observe("frigate/events", face_event("a", "Alice"), now=0)
    late.observe("frigate/events", face_event("a", "Alice", kind="end"), now=10)
    late.observe("frigate/events", face_event("b"), now=71)
    assert not late.present(72)


def test_owner_presence_expires_silent_tracks():
    presence = OwnerPresence("Alice", stale_seconds=180)
    presence.observe("frigate/events", face_event("a", "Alice"), now=0)
    presence.observe("frigate/events", face_event("a", "Alice"), now=60)
    assert presence.present(240)
    assert not presence.present(241)
    # The silent track ended when last seen, so a later retrack is not inherited.
    presence.observe("frigate/events", face_event("b"), now=245)
    assert not presence.present(246)


def test_session_gate_reports_start_before_release():
    states = []
    gate = SessionGate(on_change=states.append)
    assert gate.start("greeting", lambda: None)
    for _ in range(100):
        if len(states) == 2:
            break
        time.sleep(0.01)
    assert states == ["greeting", None]


def test_owner_presence_reports_stillness_and_motion():
    presence = OwnerPresence("Alice")
    event = face_event("a", "Alice")
    event["after"].update(stationary=False, position_changes=0)
    presence.observe("frigate/events", event, now=0)
    assert presence.still_seconds(30) == 30
    event["after"].update(stationary=True, position_changes=0)
    presence.observe("frigate/events", event, now=60)
    assert presence.still_seconds(90) == 90  # stationary update is not movement
    event["after"].update(stationary=True, position_changes=1)
    presence.observe("frigate/events", event, now=120)
    assert presence.still_seconds(150) == 30
    assert OwnerPresence("Alice").still_seconds(0) is None


def test_owner_presence_zone_filter():
    presence = OwnerPresence("Alice", zones=frozenset({"desk"}))
    event = face_event("a", "Alice")
    event["after"]["current_zones"] = ["bed"]
    presence.observe("frigate/events", event, now=0)
    assert not presence.present(1)
    event["after"]["current_zones"] = ["desk"]
    presence.observe("frigate/events", event, now=2)
    assert presence.present(3)


def test_long_motionless_presence_is_not_sitting_and_is_noted_once():
    engine = SituationEngine(SituationSettings())
    start = MONDAY_AFTERNOON
    infos = []
    for minute in range(0, 80):
        now = 10_000 + minute * 60
        # The producer only refreshes every minute; stillness grows from zero.
        engine.observe(TOPIC_PRESENCE, {"present": True, "still_seconds": minute * 60}, now)
        for decision in engine.evaluate(now, start + timedelta(minutes=minute)):
            infos.append((minute, decision.kind, decision.reason))
    assert (30, "info", "long_motionless_presence_ignored") in infos
    assert [d for d in infos if d[1] == "info"] == [(30, "info", "long_motionless_presence_ignored")]
    # The 30-minute ghost guard plus the 5-minute break reset the sitting timer
    # before the 50-minute sedentary nudge could fire.
    assert not [d for d in infos if d[1] == "nudge"]


def test_motionless_person_in_view_is_not_awake_at_night():
    engine = SituationEngine(SituationSettings())
    start = datetime(2026, 10, 20, 0, 0)  # Monday night, before bedtime
    nudges, infos = [], []
    for minute in range(0, 90):
        now = 10_000 + minute * 60
        engine.observe(TOPIC_PRESENCE, {"present": True, "still_seconds": 600 + minute * 60}, now)
        for decision in engine.evaluate(now, start + timedelta(minutes=minute)):
            (nudges if decision.kind == "nudge" else infos).append((minute, decision.reason))
    assert nudges == []
    assert (45, "asleep_night_closed") in infos


def test_moving_person_at_desk_is_awake_at_night():
    engine = SituationEngine(SituationSettings())
    start = datetime(2026, 10, 20, 0, 25)
    nudges = []
    for step in range(0, 40):
        now = 10_000 + step * 15
        engine.observe(TOPIC_PRESENCE, {"present": True, "still_seconds": 20}, now)
        for decision in engine.evaluate(now, start + timedelta(seconds=step * 15)):
            if decision.kind == "nudge":
                nudges.append(decision.nudge)
                engine.record_outcome(decision.nudge.id, "delivered", now)
    assert [n.rule for n in nudges] == [BEDTIME]
    assert nudges[0].context["evidence"] == ["desk"]


def test_camera_motion_counts_as_moving_for_a_stationary_typist():
    presence = OwnerPresence("Alice")
    event = face_event("a", "Alice")
    event["after"].update(stationary=True, position_changes=1)
    for now in range(0, 300, 60):  # Frigate's once-a-minute update for a still person
        presence.observe("frigate/events", event, now=now)
    assert presence.still_seconds(300) == 300
    presence.observe_motion("ON", now=300)
    presence.observe("frigate/events", event, now=320)
    assert presence.still_seconds(330) == 0
    presence.observe_motion("OFF", now=340)
    assert presence.still_seconds(400) == 60
    presence.observe_motion("garbage", now=401)
    assert presence.still_seconds(401) == 61


def test_final_outcome_overtaking_delivery_counts_once():
    sim = Sim(MONDAY_AFTERNOON, deliver=False)
    sim.desk = True
    sim.run(50.25)
    nudge = sim.nudges[0][1]
    engine = sim.engine
    assert engine.observe(TOPIC_NUDGE_RESULT, {"id": nudge.id, "outcome": "declined"}, sim.mono)
    assert not engine.observe(TOPIC_NUDGE_RESULT, {"id": nudge.id, "outcome": "delivered"}, sim.mono)
    assert (engine.day_count, engine.sit_nudges, engine.negatives[SEDENTARY]) == (1, 1, 1)


def test_failed_delivery_is_not_counted_or_penalized():
    sim = Sim(MONDAY_AFTERNOON, deliver=False)
    sim.desk = True
    sim.run(50.25)
    nudge = sim.nudges[0][1]
    assert sim.engine.record_outcome(nudge.id, "failed", sim.mono)
    assert (sim.engine.day_count, sim.engine.negatives.get(SEDENTARY, 0)) == (0, 0)


def test_locking_the_phone_after_a_bedtime_nudge_counts_as_accepted():
    sim = Sim(MONDAY_LATE, await_reply=True)
    sim.phone = True
    sim.run(43)
    assert sim.times() == ["00:32"]
    sim.phone = False  # screen off at 00:33
    sim.run(3)
    infos = [(t.strftime("%H:%M"), d.reason) for t, d in sim.decisions if d.kind == "info"]
    assert infos == [("00:34", "inferred_accepted")]
    assert sim.engine.negatives.get(BEDTIME, 0) == 0
    assert not sim.engine.awaiting


def test_no_change_within_the_reply_window_counts_as_ignored():
    sim = Sim(MONDAY_LATE, await_reply=True)
    sim.phone = True
    sim.run(120)
    infos = [(t.strftime("%H:%M"), d.reason) for t, d in sim.decisions if d.kind == "info"]
    assert infos[:2] == [("00:47", "inferred_ignored"), ("01:07", "inferred_ignored")]
    # Two ignores double the spacing: 00:32, 00:52, then 40 minutes later.
    assert sim.times() == ["00:32", "00:52", "01:32"]


def test_brief_screen_off_is_not_compliance():
    sim = Sim(MONDAY_LATE, await_reply=True)
    sim.phone = True
    sim.run(43)
    assert sim.times() == ["00:32"]
    sim.phone = False
    sim.run(0.5)  # 30 s is shorter than the 60 s compliance requirement
    sim.phone = True
    sim.run(2)
    assert not [d for _, d in sim.decisions if d.kind == "info"]
    assert sim.engine.awaiting


def test_explicit_answer_stops_inference_and_silent_delivery_never_waits():
    sim = Sim(MONDAY_AFTERNOON, await_reply=True, outcome="snoozed", snooze_minutes=10)
    sim.desk = True
    sim.run(51)
    assert not sim.engine.awaiting
    silent = Sim(MONDAY_AFTERNOON)
    silent.desk = True
    silent.run(51)
    assert silent.nudges and not silent.engine.awaiting


def test_leaving_the_desk_after_a_sitting_nudge_counts_as_accepted():
    sim = Sim(MONDAY_AFTERNOON, await_reply=True)
    sim.desk = True
    sim.run(51)
    sim.desk = False
    sim.run(2)
    infos = [d.reason for _, d in sim.decisions if d.kind == "info"]
    assert infos == ["inferred_accepted"]
