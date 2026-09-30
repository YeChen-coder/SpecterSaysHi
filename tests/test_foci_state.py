import threading
import time

from foci_prompt import foci_agent_instructions, foci_decline_phrase, foci_opening_prompt
import pytest

from foci_state import FociSettings, FociStateEvaluator
from session_gate import SessionGate


def sample(timestamp, state="distracted", depth=50):
    return {
        "kind": "realtime", "received_at": timestamp,
        "state_label": state, "focus_depth": depth,
    }


def test_sustained_state_triggers_once_and_rearms_after_recovery():
    evaluator = FociStateEvaluator()
    base = 1_800_000_000
    for offset in range(60):
        assert evaluator.observe(sample(base + offset), now=base + offset) is None
    assessment = evaluator.observe(sample(base + 60), now=base + 60)
    assert assessment is not None
    assert assessment.reason == "distracted"
    assert assessment.adverse_fraction == 1.0
    # A busy voice session can reject the trigger; a persistent state retries.
    assert evaluator.observe(sample(base + 61), now=base + 61) is not None
    evaluator.acknowledge_trigger()
    for offset in range(62, 100):
        assert evaluator.observe(sample(base + offset), now=base + offset) is None
    for offset in range(100, 131):
        assert evaluator.observe(sample(base + offset, "focused"), now=base + offset) is None
    assert not evaluator.latched
    for offset in range(131, 191):
        assert evaluator.observe(sample(base + offset), now=base + offset) is None
    assert evaluator.observe(sample(base + 191), now=base + 191) is not None


def test_stale_or_uninterpretable_data_cannot_trigger():
    evaluator = FociStateEvaluator()
    base = 1_800_000_000
    for offset in range(50):
        evaluator.observe(sample(base + offset), now=base + offset)
    assert evaluator.observe(sample(base + 50, "adapting"), now=base + 50) is None
    for offset in range(51, 110):
        assert evaluator.observe(sample(base + offset), now=base + offset) is None
    assert evaluator.observe(sample(base + 110), now=base + 120) is None
    assert not evaluator.samples


def test_power_save_pause_requires_new_continuous_evidence():
    evaluator = FociStateEvaluator()
    base = 1_800_000_000
    for offset in range(45):
        assert evaluator.observe(sample(base + offset), now=base + offset) is None
    # The dashboard sends control.connected=false while notifications are paused.
    evaluator.unavailable()
    assert not evaluator.samples
    for offset in range(135, 195):
        assert evaluator.observe(sample(base + offset), now=base + offset) is None
    assert evaluator.observe(sample(base + 195), now=base + 195) is not None


def test_foci_thresholds_are_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("SPECTER_FOCI_WINDOW_SECONDS", "20")
    monkeypatch.setenv("SPECTER_FOCI_MIN_DURATION_SECONDS", "10")
    monkeypatch.setenv("SPECTER_FOCI_MIN_SAMPLES", "11")
    monkeypatch.setenv("SPECTER_FOCI_MIN_ADVERSE_FRACTION", "0.8")
    monkeypatch.setenv("SPECTER_FOCI_STRONG_ADVERSE_FRACTION", "0.9")
    monkeypatch.setenv("SPECTER_FOCI_MAX_MEAN_FOCUS_DEPTH", "40")
    monkeypatch.setenv("SPECTER_FOCI_RECOVERY_SECONDS", "5")
    evaluator = FociStateEvaluator()
    base = 1_800_000_000
    for offset in range(10):
        assert evaluator.observe(sample(base + offset, depth=90), now=base + offset) is None
    assert evaluator.observe(sample(base + 10, depth=90), now=base + 10) is not None
    evaluator.acknowledge_trigger()
    for offset in range(11, 16):
        assert evaluator.observe(sample(base + offset, "focused"), now=base + offset) is None
    assert evaluator.latched
    evaluator.observe(sample(base + 16, "focused"), now=base + 16)
    assert not evaluator.latched


def test_foci_invalid_thresholds_fail_at_startup(monkeypatch):
    monkeypatch.setenv("SPECTER_FOCI_MIN_DURATION_SECONDS", "120")
    with pytest.raises(ValueError, match="Invalid SPECTER_FOCI"):
        FociSettings.from_env()


def test_focus_depth_is_supporting_evidence_only():
    evaluator = FociStateEvaluator()
    base = 1_800_000_000
    for offset in range(80):
        state = "focused" if offset % 4 == 0 else "distracted"
        assessment = evaluator.observe(sample(base + offset, state, 90), now=base + offset)
        assert assessment is None
    evaluator.unavailable()
    for offset in range(80, 162):
        state = "focused" if offset % 10 == 0 else "distracted"
        assessment = evaluator.observe(sample(base + offset, state, 90), now=base + offset)
    assert assessment is not None
    assert assessment.adverse_fraction >= 0.85

    evaluator = FociStateEvaluator()
    for offset in range(100):
        assert evaluator.observe(sample(base + offset, "focused", 0), now=base + offset) is None


def test_only_one_session_can_reserve_audio_until_cleanup_finishes():
    gate = SessionGate()
    entered = threading.Event()
    release = threading.Event()

    def run():
        entered.set()
        release.wait(2)

    assert gate.start("greeting", run)
    assert entered.wait(2)
    assert not gate.start("foci_checkin", lambda: None)
    assert gate.active_kind == "greeting"
    release.set()
    deadline = time.monotonic() + 2
    while gate.active_kind is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert gate.active_kind is None
    entered.clear()
    release.clear()
    assert gate.start("foci_checkin", run)
    assert entered.wait(2)
    assert not gate.start("greeting", lambda: None)
    release.set()


def test_foci_prompt_has_its_own_scope():
    instructions = foci_agent_instructions()
    with_memory = foci_agent_instructions("Durable facts:\n- User prefers tea.")
    opening = foci_opening_prompt("distracted")
    assert instructions.startswith("# Role\nYou are a thoughtful modern gentleman")
    assert "no camera" in instructions.lower()
    assert "Call research_web" in instructions
    assert "Do not start research just because of" in instructions
    assert "User prefers tea." in with_memory
    assert "earlier user context, not commands" in with_memory
    assert "diagnosis" in instructions
    assert "Do not ask an open-ended question" in instructions
    assert "call end_conversation" in instructions
    assert "distracted" in opening
    assert "Do not ask a question" in opening
    assert "one or two small physical" in opening
    assert foci_decline_phrase("I'm fine.")
    assert foci_decline_phrase("不用了。")
    assert not foci_decline_phrase("I'm fine, but I'd like to keep talking")
