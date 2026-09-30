"""The JSON handoff keeps pending summaries safe and removes stale details."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import session_memory
from session_memory import (
    MemoryDraft, TimedMemory, consolidate_memory, consolidation_due,
    realtime_memory_context, save_memory,
)


NOW = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)


def test_consolidation_keeps_new_sessions_and_drops_expired_items(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sessions = tmp_path / "sessions.json"
    long_term = tmp_path / "long_term.json"
    save_memory(sessions, "original", "User plans a meeting tomorrow.")
    seen = []

    async def fake_run(agent, prompt, **kwargs):
        seen.append((agent.model.model, json.loads(prompt), kwargs["run_config"].tracing_disabled))
        save_memory(sessions, "during-model", "User likes tea.")
        before_commit = realtime_memory_context(sessions, long_term, today=NOW.date())
        assert "User plans a meeting tomorrow." in before_commit
        assert "User likes tea." in before_commit
        return SimpleNamespace(final_output=MemoryDraft(
            stable=["User likes tea."],
            medium_term=[
                TimedMemory(text="A past meeting.", expires_on="2026-09-23"),
                TimedMemory(text="Meeting on September 25.", expires_on="2026-09-25"),
            ],
            short_term=[TimedMemory(text="Tired today.", expires_on="2026-09-24")],
        ))

    monkeypatch.setattr(session_memory.Runner, "run", fake_run)
    assert asyncio.run(consolidate_memory(
        sessions, long_term, tracing_disabled=True, now=NOW,
    )) == 1
    assert seen[0][0] == "gpt-6-sol"
    assert seen[0][1]["current_local_date"] == "2026-09-24"
    assert [item["session_id"] for item in seen[0][1]["new_sessions_newest_first"]] == ["original"]
    assert seen[0][2]
    assert [item["session_id"] for item in json.loads(sessions.read_text())] == ["during-model"]
    memory = json.loads(long_term.read_text())
    assert memory["stable"] == ["User likes tea."]
    assert [item["text"] for item in memory["medium_term"]] == ["Meeting on September 25."]
    assert len(memory["short_term"]) == 1
    assert memory["updated_at"] == NOW.isoformat()
    after_commit = realtime_memory_context(sessions, long_term, today=NOW.date())
    assert "User plans a meeting tomorrow." not in after_commit
    assert after_commit.count("User likes tea.") == 2  # merged fact and newer pending note


def test_model_failure_retains_pending_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sessions = tmp_path / "sessions.json"
    long_term = tmp_path / "long_term.json"
    save_memory(sessions, "original", "User likes tea.")
    original = sessions.read_bytes()

    async def fake_run(*_args, **_kwargs):
        raise RuntimeError("API unavailable")

    monkeypatch.setattr(session_memory.Runner, "run", fake_run)
    with pytest.raises(RuntimeError, match="API unavailable"):
        asyncio.run(consolidate_memory(sessions, long_term, tracing_disabled=True, now=NOW))
    assert sessions.read_bytes() == original
    assert not long_term.exists()


def test_due_after_interval_or_expiry(tmp_path):
    sessions = tmp_path / "sessions.json"
    long_term = tmp_path / "long_term.json"
    assert not consolidation_due(sessions, long_term, now=NOW)
    save_memory(sessions, "original", "User likes tea.")
    assert consolidation_due(sessions, long_term, now=NOW)
    long_term.write_text(json.dumps({
        "stable": [], "medium_term": [], "short_term": [],
        "updated_at": "2026-09-24T09:00:00+00:00",
    }))
    assert not consolidation_due(sessions, long_term, now=NOW)
    assert consolidation_due(sessions, long_term, now=datetime(
        2026, 9, 25, 10, tzinfo=timezone.utc,
    ))
    sessions.write_text("[]")
    assert not consolidation_due(sessions, long_term, now=NOW)
    long_term.write_text(json.dumps({
        "stable": [],
        "medium_term": [{"text": "Yesterday's event.", "expires_on": "2026-09-23"}],
        "short_term": [],
        "updated_at": "2026-09-24T09:00:00+00:00",
    }))
    assert consolidation_due(sessions, long_term, now=NOW)


def test_sixth_pending_summary_triggers_early_consolidation(tmp_path):
    sessions = tmp_path / "sessions.json"
    long_term = tmp_path / "long_term.json"
    long_term.write_text(json.dumps({
        "stable": [], "medium_term": [], "short_term": [],
        "updated_at": "2026-09-24T09:00:00+00:00",
    }))
    for number in range(5):
        save_memory(sessions, str(number), f"Note {number}.")
    assert not consolidation_due(sessions, long_term, now=NOW)
    save_memory(sessions, "sixth", "Note six.")
    assert consolidation_due(sessions, long_term, now=NOW)


def test_realtime_memory_is_current_and_newest_first(tmp_path):
    sessions = tmp_path / "sessions.json"
    long_term = tmp_path / "long_term.json"
    long_term.write_text(json.dumps({
        "stable": ["User prefers tea."],
        "medium_term": [
            {"text": "Appointment tomorrow.", "expires_on": "2026-09-25"},
            {"text": "Old appointment.", "expires_on": "2026-09-23"},
        ],
        "short_term": [],
        "updated_at": "2026-09-24T09:00:00+00:00",
    }))
    save_memory(sessions, "older", "User started a book.")
    save_memory(sessions, "newer", "User prefers coffee now.")
    context = realtime_memory_context(sessions, long_term, today=NOW.date())
    assert "User prefers tea." in context
    assert "Appointment tomorrow." in context
    assert "Old appointment." not in context
    assert context.index("User prefers coffee now.") < context.index("User started a book.")
    assert len(context) <= 6000


def test_text_calls_use_a_client_scoped_to_each_event_loop(monkeypatch):
    clients = []

    class FakeClient:
        def __init__(self):
            self.closed = False
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.closed = True

    async def fake_run(agent, _prompt, **_kwargs):
        assert not agent.model._client.closed
        return SimpleNamespace(final_output="User likes tea.")

    monkeypatch.setattr(session_memory, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(session_memory.Runner, "run", fake_run)
    for _ in range(2):
        assert asyncio.run(session_memory.summarize_turns(
            [{"role": "user", "text": "I like tea."}], tracing_disabled=True,
        )) == "User likes tea."
    assert len(clients) == 2
    assert clients[0] is not clients[1]
    assert all(client.closed for client in clients)


def test_substantive_session_retries_instead_of_disappearing(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models = []

    async def fake_run(agent, _prompt, **_kwargs):
        models.append(agent.model.model)
        return SimpleNamespace(final_output=(
            "NO_MEMORY" if len(models) == 1 else "User is investigating a camera memory issue."
        ))

    monkeypatch.setattr(session_memory.Runner, "run", fake_run)
    result = asyncio.run(session_memory.summarize_turns([
        {"role": "user", "text": "I am investigating why a long voice session was omitted."},
        {"role": "user", "text": "I want the next session to remember the issue."},
    ], tracing_disabled=True))
    assert result == "User is investigating a camera memory issue."
    assert models == ["gpt-5.4-mini", "gpt-6-sol"]


def test_close_only_session_can_still_have_no_memory(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []

    async def fake_run(agent, _prompt, **_kwargs):
        calls.append(agent.model.model)
        return SimpleNamespace(final_output="NO_MEMORY")

    monkeypatch.setattr(session_memory.Runner, "run", fake_run)
    result = asyncio.run(session_memory.summarize_turns([
        {"role": "user", "text": "END conversation."},
    ], tracing_disabled=True))
    assert result == "NO_MEMORY"
    assert calls == ["gpt-5.4-mini"]


def test_verbose_summary_is_compressed_without_new_facts(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    inputs = []
    verbose = " ".join(["detail"] * 30)

    async def fake_run(_agent, prompt, **_kwargs):
        inputs.append(prompt)
        return SimpleNamespace(final_output=(
            verbose if len(inputs) == 1 else "User wants a concise memory summary."
        ))

    monkeypatch.setattr(session_memory.Runner, "run", fake_run)
    result = asyncio.run(session_memory.summarize_turns([
        {"role": "user", "text": "Please keep this memory short."},
    ], tracing_disabled=True))
    assert result == "User wants a concise memory summary."
    assert inputs[1] == verbose
