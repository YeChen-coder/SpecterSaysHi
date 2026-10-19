"""Phase 2: the situation-engine nudge session (no audio devices or network)."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from nudge_prompt import nudge_agent_instructions, nudge_opening_prompt
from realtime_agent import RealtimeConversation, session_time_limits
from test_realtime_events import FakeSession, raw_event

BEDTIME_NUDGE = {
    "id": "abc123", "rule": "bedtime", "level": 2, "channel": "desk",
    "context": {
        "minutes_past_bedtime": 22, "evidence": ["desk", "phone"],
        "phone_app": "tv.danmaku.bili", "foci_label": "fatigued",
        "previous_nudges_tonight": 1,
    },
}
SITTING_NUDGE = {
    "id": "def456", "rule": "sedentary", "level": 1, "channel": "desk",
    "context": {"sitting_minutes": 52, "foci_label": "flow"},
}


def test_opening_prompt_uses_level_and_coarse_facts_only():
    opening = nudge_opening_prompt(BEDTIME_NUDGE)
    assert "second bedtime reminder" in opening
    assert "22 minutes past" in opening
    assert "still at the desk" in opening
    # App names and wearable labels stay on this computer.
    assert "bili" not in opening
    assert "fatigued" not in opening
    sitting = nudge_opening_prompt(SITTING_NUDGE)
    assert "52 minutes" in sitting
    assert "flow" not in sitting


def test_opening_prompt_clamps_level_and_rejects_unknown_rules():
    assert "last bedtime reminder" in nudge_opening_prompt({**BEDTIME_NUDGE, "level": 9})
    assert "first bedtime reminder" in nudge_opening_prompt({**BEDTIME_NUDGE, "level": 0})
    assert "none." in nudge_opening_prompt({"rule": "sedentary", "level": 1, "context": {}})
    with pytest.raises(ValueError):
        nudge_opening_prompt({"rule": "hydration", "level": 1})


def test_instructions_forbid_explaining_the_sensors():
    instructions = nudge_agent_instructions("User sleeps better after reading.")
    assert "Never mention" in instructions and "cameras" in instructions
    assert "respond_to_reminder" in instructions
    assert "no camera image" in instructions
    assert "User sleeps better after reading." in instructions


def test_nudge_session_requires_a_request():
    with pytest.raises(ValueError):
        RealtimeConversation("Mira", session_kind="nudge")
    with pytest.raises(ValueError):
        RealtimeConversation("Mira", nudge=BEDTIME_NUDGE)


def test_nudge_session_opening_agent_and_no_camera(tmp_path):
    async def scenario():
        conversation = RealtimeConversation(
            "Mira", session_kind="nudge", nudge=BEDTIME_NUDGE,
            frame_source=object(), memory_path=tmp_path / "sessions.json",
        )
        assert conversation.frame_source is None
        session = FakeSession()
        await conversation._on_event(session, raw_event("session.updated"))
        assert [event["type"] for event in session.model.events] == ["response.create"]
        opening = session.model.events[0]["other_data"]["response"]["instructions"]
        assert "second bedtime reminder" in opening
        agent = conversation.build_agent()
        assert agent.name == "Situation nudge"
        assert [tool.name for tool in agent.tools] == ["respond_to_reminder", "end_conversation"]

    asyncio.run(scenario())


def invoke(tool, arguments):
    context = SimpleNamespace(tool_name=tool.name, _function_tool_arguments=None)
    return asyncio.run(tool.on_invoke_tool(context, json.dumps(arguments)))


def test_reminder_tool_records_snooze_with_bounds():
    conversation = RealtimeConversation("Mira", session_kind="nudge", nudge=SITTING_NUDGE)
    tool = conversation.make_reminder_response_tool()
    assert "between 5 and 60" in invoke(tool, {"decision": "snooze"})
    assert conversation.nudge_decision is None
    assert "15 minutes" in invoke(tool, {"decision": "snooze", "snooze_minutes": 15})
    assert conversation.nudge_outcome() == ("snoozed", 15)
    invoke(tool, {"decision": "snooze", "snooze_minutes": 240})
    assert conversation.nudge_outcome() == ("snoozed", 60)
    assert "Use decision" in invoke(tool, {"decision": "maybe"})
    invoke(tool, {"decision": "Accept"})
    assert conversation.nudge_outcome() == ("accepted", None)


def transcript(conversation, item_id, text):
    asyncio.run(conversation._on_event(FakeSession(), raw_event(
        "conversation.item.input_audio_transcription.completed",
        item_id=item_id, transcript=text,
    )))


def test_outcome_without_tool_call_falls_back_to_what_was_heard():
    silent = RealtimeConversation("Mira", session_kind="nudge", nudge=BEDTIME_NUDGE)
    assert silent.nudge_outcome() == ("ignored", None)

    replied = RealtimeConversation("Mira", session_kind="nudge", nudge=BEDTIME_NUDGE)
    transcript(replied, "a", "Okay, finishing this episode first.")
    assert replied.nudge_outcome() == ("accepted", None)

    declined = RealtimeConversation("Mira", session_kind="nudge", nudge=BEDTIME_NUDGE)
    transcript(declined, "b", "not now")
    assert declined.nudge_outcome() == ("declined", None)


def test_nudge_opening_uses_its_own_short_idle_limit(monkeypatch):
    monkeypatch.delenv("SPECTER_NUDGE_SESSION_IDLE_SECONDS", raising=False)
    monkeypatch.setenv("SPECTER_FOCI_SESSION_IDLE_SECONDS", "15")
    assert session_time_limits(True, "nudge")[0] == 20
    assert session_time_limits(True, "foci_checkin")[0] == 15
    monkeypatch.setenv("SPECTER_NUDGE_SESSION_IDLE_SECONDS", "30")
    assert session_time_limits(True, "nudge") == (30, 300)
