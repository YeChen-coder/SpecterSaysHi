"""Close-session control and newest-first JSON memory behavior."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import realtime_agent
from realtime_agent import RealtimeConversation, agent_instructions
from session_control import (
    clear_active, close_requested, consume_start_request, explicit_close_phrase, publish_active,
    publish_close_status, read_close_status, request_close, request_start, wait_for_close,
)
from session_memory import save_memory


class FakeSession:
    def __init__(self):
        self.model = SimpleNamespace(send_event=self._send_event)

    async def _send_event(self, _event):
        pass


def raw_event(kind, **fields):
    return SimpleNamespace(
        type="raw_model_event",
        data=SimpleNamespace(type="raw_server_event", data={"type": kind, **fields}),
    )


def test_close_request_targets_only_the_active_session(tmp_path):
    assert request_close(tmp_path) is None
    publish_active(tmp_path, "first")
    assert request_close(tmp_path) == "first"
    assert close_requested(tmp_path, "first")
    assert not close_requested(tmp_path, "second")
    publish_active(tmp_path, "second")
    clear_active(tmp_path, "first")
    assert (tmp_path / "active_session.json").exists()
    assert not close_requested(tmp_path, "second")
    clear_active(tmp_path, "second")
    assert not (tmp_path / "active_session.json").exists()


def test_manual_start_request_is_consumed_once(tmp_path):
    assert consume_start_request(tmp_path) is None
    request_id = request_start(tmp_path)
    assert consume_start_request(tmp_path) == request_id
    assert consume_start_request(tmp_path) is None


def test_close_command_waits_for_memory_result(tmp_path, capsys):
    publish_active(tmp_path, "current")
    assert request_close(tmp_path) == "current"
    assert read_close_status(tmp_path, "current")["stage"] == "transcribing"

    def finish():
        time.sleep(0.05)
        publish_close_status(tmp_path, "current", "saving")
        time.sleep(0.1)
        publish_close_status(tmp_path, "current", "done", "saved")

    worker = threading.Thread(target=finish)
    worker.start()
    assert wait_for_close(tmp_path, "current", timeout=1) == 0
    worker.join()
    output = capsys.readouterr().out
    assert "Finishing transcription" in output
    assert "Saving session memory" in output
    assert "Session memory saved" in output


def test_manual_request_stops_current_conversation(tmp_path):
    async def scenario():
        conversation = RealtimeConversation("Mira", control_dir=tmp_path)
        publish_active(tmp_path, conversation.local_session_id)
        assert request_close(tmp_path) == conversation.local_session_id
        await asyncio.wait_for(conversation._watchdog(), timeout=2)
        assert conversation.stop.is_set()

    asyncio.run(scenario())


def test_foci_checkin_closes_quickly_when_silent_but_not_during_speech(tmp_path):
    async def scenario():
        conversation = RealtimeConversation("Mira", control_dir=tmp_path, session_kind="foci_checkin")
        conversation.ready.set()
        conversation.last_activity = time.monotonic() - 16
        conversation.microphone_speaking = True
        task = asyncio.create_task(conversation._watchdog())
        await asyncio.sleep(1.1)
        assert not conversation.stop.is_set()
        conversation.microphone_speaking = False
        await asyncio.wait_for(task, timeout=2)
        assert conversation.stop.is_set()

    asyncio.run(scenario())


def test_voice_tool_closes_after_goodbye_finishes_playing():
    async def scenario():
        conversation = RealtimeConversation("Mira")
        tool = conversation.make_end_conversation_tool()
        assert tool.name == "end_conversation"
        assert "call end_conversation immediately" in agent_instructions("Mira", False)
        conversation.response_active = True
        conversation.tools_in_progress = 1
        tool_context = SimpleNamespace(tool_name=tool.name, _function_tool_arguments=None)
        result = await tool.on_invoke_tool(tool_context, "{}")
        assert "Say one short goodbye" in result
        assert conversation.voice_close_requested_at is not None
        assert not conversation.stop.is_set()

        conversation.speaker_busy = True
        await conversation._on_event(FakeSession(), SimpleNamespace(type="tool_end"))
        await conversation._on_event(
            FakeSession(),
            raw_event("response.output_audio_transcript.done", item_id="goodbye", transcript="Goodbye."),
        )
        await conversation._on_event(FakeSession(), SimpleNamespace(
            type="raw_model_event", data=SimpleNamespace(type="turn_ended"),
        ))
        assert not conversation.stop.is_set()
        conversation._mark_playback_complete()
        assert conversation.stop.is_set()

    asyncio.run(scenario())


def test_voice_close_times_out_if_goodbye_never_arrives(tmp_path):
    async def scenario():
        conversation = RealtimeConversation("Mira", control_dir=tmp_path)
        conversation.voice_close_requested_at = time.monotonic() - 13
        await asyncio.wait_for(conversation._watchdog(), timeout=2)
        assert conversation.stop.is_set()

    asyncio.run(scenario())


def test_direct_end_conversation_phrase_has_local_fallback(tmp_path):
    assert explicit_close_phrase("END conversation.")
    assert explicit_close_phrase("Please end this conversation now.")
    assert explicit_close_phrase("结束这次对话吧")
    assert not explicit_close_phrase("What does end conversation mean?")

    async def scenario():
        conversation = RealtimeConversation("Mira", control_dir=tmp_path)
        await conversation._on_event(FakeSession(), raw_event(
            "conversation.item.input_audio_transcription.completed",
            item_id="end-command", transcript="END conversation.",
        ))
        assert conversation.explicit_close_transcript_at is not None
        conversation.explicit_close_transcript_at = time.monotonic() - 4
        await asyncio.wait_for(conversation._watchdog(), timeout=2)
        assert conversation.stop.is_set()

    asyncio.run(scenario())


def test_completed_dialogue_is_summarized_and_newest_memory_is_first(tmp_path, monkeypatch):
    captured = []
    session_ids = []
    monkeypatch.setenv("SPECTER_TRACE_ENABLED", "false")

    async def fake_summarize(turns, *, tracing_disabled):
        captured.extend(turns)
        assert tracing_disabled
        return "Mira prefers concise updates.\nFollow up on the camera setup."

    monkeypatch.setattr(realtime_agent, "summarize_turns", fake_summarize)
    memory_file = tmp_path / "memory" / "sessions.json"
    memory_saved = threading.Event()
    save_memory(memory_file, "older", "An earlier discussion.")

    async def scenario():
        conversation = RealtimeConversation(
            "Mira", memory_path=memory_file, memory_saved_event=memory_saved,
        )
        session_ids.append(conversation.local_session_id)
        session = FakeSession()
        await conversation._on_event(
            session,
            raw_event("response.output_audio_transcript.done", item_id="greeting", transcript="Hello."),
        )
        await conversation._on_event(
            session,
            raw_event("conversation.item.input_audio_transcription.delta", item_id="user-1", delta="Please"),
        )
        assert not conversation.memory_turns[1:]
        await conversation._on_event(session, SimpleNamespace(
            type="raw_model_event",
            data=SimpleNamespace(
                type="input_audio_transcription_completed",
                item_id="user-1", transcript="Please remember my camera setup.",
            ),
        ))
        await conversation._on_event(
            session,
            raw_event("response.output_audio_transcript.done", item_id="answer", transcript="I can help with that."),
        )
        await conversation._save_memory()

    asyncio.run(scenario())
    entries = json.loads(memory_file.read_text(encoding="utf-8"))
    assert memory_saved.is_set()
    assert [entry["session_id"] for entry in entries] == [session_ids[0], "older"]
    assert entries[0]["summary"] == "Mira prefers concise updates.\nFollow up on the camera setup."
    assert "Please remember my camera setup." not in memory_file.read_text(encoding="utf-8")
    assert [turn["role"] for turn in captured] == ["assistant", "user", "assistant"]


def test_foci_dialogue_saves_to_the_same_sessions_file(tmp_path, monkeypatch):
    memory_file = tmp_path / "memory" / "sessions.json"
    save_memory(memory_file, "greeting-session", "User prefers concise updates.")

    async def fake_summarize(turns, *, tracing_disabled):
        assert any(turn["role"] == "user" for turn in turns)
        return "User asked about a current topic during a FOCI check-in."

    monkeypatch.setattr(realtime_agent, "summarize_turns", fake_summarize)
    conversation = RealtimeConversation(
        "Mira", session_kind="foci_checkin", memory_path=memory_file,
    )
    conversation.memory_turns.append({"role": "user", "text": "Please look up the latest update."})
    assert asyncio.run(conversation._save_memory()) == "saved"
    entries = json.loads(memory_file.read_text(encoding="utf-8"))
    assert [entry["session_id"] for entry in entries] == [
        conversation.local_session_id, "greeting-session",
    ]


def test_summary_failure_keeps_existing_memory(tmp_path, monkeypatch):
    memory_file = tmp_path / "sessions.json"
    save_memory(memory_file, "older", "An earlier discussion.")
    original = memory_file.read_bytes()

    async def fail_summary(_turns, *, tracing_disabled):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(realtime_agent, "summarize_turns", fail_summary)
    conversation = RealtimeConversation("Mira", memory_path=memory_file)
    conversation.memory_turns.append({"role": "user", "text": "New information"})
    asyncio.run(conversation._save_memory())
    assert memory_file.read_bytes() == original


def test_partial_transcription_is_saved_if_session_closes_early(tmp_path, monkeypatch):
    captured = []

    async def fake_summarize(turns, *, tracing_disabled):
        captured.extend(turns)
        return "User mentioned a possible appointment."

    monkeypatch.setattr(realtime_agent, "summarize_turns", fake_summarize)
    memory_file = tmp_path / "sessions.json"

    async def scenario():
        conversation = RealtimeConversation("Mira", memory_path=memory_file)
        await conversation._on_event(FakeSession(), raw_event(
            "conversation.item.input_audio_transcription.delta",
            item_id="unfinished", delta="I might have an appointment",
        ))
        assert not conversation.memory_turns
        await conversation._save_memory()

    asyncio.run(scenario())
    assert captured == [{
        "role": "user", "text": "[Incomplete transcription] I might have an appointment",
    }]
    assert len(json.loads(memory_file.read_text())) == 1


def test_raw_completed_transcription_replaces_partial_once():
    async def scenario():
        conversation = RealtimeConversation("Mira")
        session = FakeSession()
        await conversation._on_event(session, raw_event(
            "conversation.item.input_audio_transcription.delta", item_id="one", delta="I like",
        ))
        completed = raw_event(
            "conversation.item.input_audio_transcription.completed",
            item_id="one", transcript="I like tea.",
        )
        await conversation._on_event(session, completed)
        await conversation._on_event(session, completed)
        assert conversation.memory_turns == [{"role": "user", "text": "I like tea."}]
        assert not conversation.partial_user_transcripts

    asyncio.run(scenario())


def test_manual_close_waits_briefly_for_active_speech(tmp_path):
    async def scenario():
        conversation = RealtimeConversation("Mira", control_dir=tmp_path)
        publish_active(tmp_path, conversation.local_session_id)
        conversation.microphone_speaking = True
        request_close(tmp_path)
        watchdog = asyncio.create_task(conversation._watchdog())
        await asyncio.sleep(1.1)
        assert not conversation.stop.is_set()
        conversation.microphone_speaking = False
        await asyncio.wait_for(watchdog, timeout=3)
        assert conversation.stop.is_set()

    asyncio.run(scenario())


def test_manual_close_waits_for_completed_transcription(tmp_path):
    async def scenario():
        conversation = RealtimeConversation("Mira", control_dir=tmp_path)
        session = FakeSession()
        publish_active(tmp_path, conversation.local_session_id)
        await conversation._on_event(session, raw_event("input_audio_buffer.speech_stopped"))
        assert conversation.pending_transcriptions == 1
        request_close(tmp_path)
        watchdog = asyncio.create_task(conversation._watchdog())
        await asyncio.sleep(1.1)
        assert not conversation.stop.is_set()
        await conversation._on_event(session, raw_event(
            "conversation.item.input_audio_transcription.completed",
            item_id="spoken", transcript="I prefer jasmine tea.",
        ))
        assert conversation.pending_transcriptions == 0
        await asyncio.wait_for(watchdog, timeout=2)
        assert conversation.memory_turns == [{"role": "user", "text": "I prefer jasmine tea."}]

    asyncio.run(scenario())
