"""Wake phrase matching, utterance segmentation, and the wake-started conversation."""

import asyncio

import numpy as np
import pytest

from realtime_agent import RealtimeConversation, session_time_limits
from test_realtime_events import FakeSession, raw_event
from wake_word import UtteranceSegmenter, WakePhraseMatcher


@pytest.mark.parametrize("heard, remainder", [
    ("Hey Specter.", ""),
    ("Hi Specter.", ""),
    ("High Specter.", ""),               # how Whisper heard a non-native "Hi"
    ("Hey, Spectre!", ""),
    ("hey spector", ""),
    ("Hi, Specter. Are you there?", "are you there"),
    ("Hey Specter, how was your day?", "how was your day"),
])
def test_wake_phrases_and_variants(heard, remainder):
    assert WakePhraseMatcher().match(heard) == remainder


@pytest.mark.parametrize("heard", [
    "Hey. Check this out.",
    "I need to inspect the code first.",
    "Open the Spectrum Analyzer.",
    "The Specter project is almost done.",
    "Hi there. Nice to meet you.",
    "Specter, hello.",                   # name without a greeting needs the opt-in
    "",
])
def test_ordinary_speech_does_not_wake(heard):
    assert WakePhraseMatcher().match(heard) is None


def test_name_alone_can_be_allowed():
    assert WakePhraseMatcher(require_greeting=False).match("Specter, hello.") == "hello"


def tone(ms, amplitude):
    n = 16 * ms
    return (amplitude * np.sin(np.arange(n) * 0.2)).astype(np.int16)


def feed(segmenter, audio):
    out = []
    for start in range(0, len(audio) - 479, 480):
        segment = segmenter.feed(audio[start:start + 480])
        if segment is not None:
            out.append(segment)
    return out


def test_segmenter_cuts_a_short_utterance_after_a_pause():
    segmenter = UtteranceSegmenter()
    audio = np.concatenate([tone(900, 50), tone(1200, 6000), tone(900, 50)])
    segments = feed(segmenter, audio)
    assert len(segments) == 1
    assert 1.2 <= len(segments[0]) / 16000 <= 2.2   # speech plus pre-roll and the closing pause


def test_segmenter_ignores_clicks_and_keeps_only_the_start_of_long_talk():
    segmenter = UtteranceSegmenter()
    click = np.concatenate([tone(900, 50), tone(60, 8000), tone(900, 50)])
    assert feed(segmenter, click) == []
    long_talk = np.concatenate([tone(900, 50), tone(15000, 6000), tone(900, 50)])
    segments = feed(UtteranceSegmenter(), long_talk)
    assert len(segments) == 1 and len(segments[0]) / 16000 <= 6.1


def test_wake_conversation_opens_with_the_request_and_closes_if_unanswered(monkeypatch):
    monkeypatch.delenv("SPECTER_GREETING_SESSION_IDLE_SECONDS", raising=False)
    assert session_time_limits(True, "wake") == (15, 300)

    async def scenario():
        conversation = RealtimeConversation("Mira", session_kind="wake", wake_text="how was your day",
                                            frame_source=object())
        assert conversation.frame_source is not None
        session = FakeSession()
        conversation._refresh_visual_context = lambda _session: asyncio.sleep(0)
        await conversation._on_event(session, raw_event("session.updated"))
        opening = session.model.events[-1]["other_data"]["response"]["instructions"]
        assert "called you by name" in opening and "how was your day" in opening
        bare = RealtimeConversation("Mira", session_kind="wake")
        bare._refresh_visual_context = lambda _session: asyncio.sleep(0)
        session2 = FakeSession()
        await bare._on_event(session2, raw_event("session.updated"))
        assert "brief, natural acknowledgement" in session2.model.events[-1]["other_data"]["response"]["instructions"]

    asyncio.run(scenario())
