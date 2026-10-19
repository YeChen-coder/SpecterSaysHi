"""Camera cover do-not-disturb, other apps' dictation, and short greeting sessions."""

import asyncio
import io

from PIL import Image

from availability import CoverDetector, dictating_app, frame_brightness
from realtime_agent import RealtimeConversation, session_time_limits
from test_realtime_events import FakeSession, raw_event


def jpeg(level):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 36), (level, level, level)).save(buffer, "JPEG")
    return buffer.getvalue()


def test_frame_brightness_matches_measured_cover_states():
    assert frame_brightness(jpeg(8)) < 25 < frame_brightness(jpeg(113))


def test_cover_needs_ten_dark_seconds_and_three_bright_ones():
    cover = CoverDetector()
    assert cover.observe(7, 0) is None
    assert cover.observe(7, 9) is None          # a hand over the lens is not enough
    assert cover.observe(7, 10) is True
    assert cover.covered
    assert cover.observe(113, 11) is None
    assert cover.observe(7, 12) is None         # flicker resets the opening timer
    assert cover.observe(113, 13) is None
    assert cover.observe(113, 16) is False
    assert not cover.covered


def test_dictation_apps_are_recognized_and_specter_is_ignored(monkeypatch):
    monkeypatch.delenv("SPECTER_DICTATION_APPS", raising=False)
    typeless = r"C:#Users#me#AppData#Local#Programs#Typeless#Typeless.exe"
    specter = r"D:#Apps#SpecterSaysHi#.venv#Scripts#python.exe"
    assert dictating_app([typeless]) == "Typeless.exe"
    assert dictating_app(["OpenAI.Codex_2p2nqsd0c76g0"]) == "OpenAI.Codex_2p2nqsd0c76g0"
    assert dictating_app([r"C:#Program Files#Microsoft#Edge#msedge.exe"]) == "msedge.exe"
    assert dictating_app([specter], own_executable=r"D:\Apps\SpecterSaysHi\.venv\Scripts\python.exe") is None
    assert dictating_app([r"C:#Games#game.exe"]) is None
    monkeypatch.setenv("SPECTER_DICTATION_APPS", "zoom")
    assert dictating_app([typeless]) is None


def test_greeting_without_a_reply_closes_quickly(monkeypatch):
    monkeypatch.delenv("SPECTER_GREETING_SESSION_IDLE_SECONDS", raising=False)
    assert session_time_limits(True, "greeting") == (15, 300)
    conversation = RealtimeConversation("Mira")
    asyncio.run(conversation._on_event(FakeSession(), raw_event(
        "conversation.item.input_audio_transcription.completed",
        item_id="reply", transcript="Hi, long day.",
    )))
    assert conversation.greeting_reply_started


def test_interrupt_reason_ends_the_session():
    conversation = RealtimeConversation("Mira", interrupt=lambda: "Typeless.exe started using the microphone")
    asyncio.run(asyncio.wait_for(conversation._watchdog(), 5))
    assert conversation.stop.is_set()


def test_failing_availability_check_keeps_the_session(monkeypatch):
    def broken():
        raise OSError("registry unavailable")

    conversation = RealtimeConversation("Mira", interrupt=broken)

    async def scenario():
        task = asyncio.create_task(conversation._watchdog())
        await asyncio.sleep(1.5)
        stopped = conversation.stop.is_set()
        conversation.stop.set()
        await asyncio.wait_for(task, 3)
        return stopped

    assert asyncio.run(scenario()) is False


def test_local_barge_in_ignores_echo_and_catches_a_voice():
    from realtime_agent import LocalBargeIn

    barge = LocalBargeIn(warmup_s=1.0, factor=3.0, min_rms=500, chunks=3)
    barge.reset(0.0)
    # Echo after cancelling hovers around 300 while he speaks.
    for step in range(10):
        assert barge.observe(300 + (step % 3) * 40, 0.1 * step) is False
    for step in range(10, 30):
        assert barge.observe(320, 0.1 * step) is False          # echo never trips it
    assert barge.observe(2000, 3.0) is False                    # a single bump (a cough) is not enough
    assert barge.observe(300, 3.1) is False
    assert [barge.observe(1800, 3.2 + 0.1 * i) for i in range(3)] == [False, False, True]


def test_barge_in_mode_defaults_to_local(monkeypatch):
    from realtime_agent import barge_in_enabled, barge_in_mode

    monkeypatch.delenv("SPECTER_BARGE_IN_MODE", raising=False)
    monkeypatch.delenv("SPECTER_BARGE_IN_ENABLED", raising=False)
    assert barge_in_mode() == "local" and not barge_in_enabled()
    monkeypatch.setenv("SPECTER_BARGE_IN_ENABLED", "true")
    assert barge_in_mode() == "server" and barge_in_enabled()
    monkeypatch.setenv("SPECTER_BARGE_IN_MODE", "off")
    assert barge_in_mode() == "off"
    conversation = RealtimeConversation("Mira")
    assert conversation.local_barge is None


def test_dark_frame_is_not_sent_as_vision():
    class DarkCamera:
        def latest_jpeg(self):
            return jpeg(5)

    async def scenario():
        conversation = RealtimeConversation("Mira", frame_source=DarkCamera())
        session = FakeSession()
        await conversation._refresh_visual_context(session)
        return session.model.events

    assert asyncio.run(scenario()) == []
