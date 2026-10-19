"""Lockdown protocol, allow list, and strict texts (no device, no network)."""

import json
import threading
import urllib.request
from datetime import datetime, time

import pytest

from nudge_prompt import (
    STRICT_CAUGHT_FALLBACK, phone_fallback_text, phone_nudge_payload, phone_text_request,
)
from phone_guard import DEFAULT_ALLOW, allow_list, guard_payload, next_morning
from phone_link import PhoneLink, make_server, validate_guard, validate_nudge


def strict_nudge(level, **context):
    return {"id": "s1", "rule": "bedtime", "level": level, "context": {"strict": True, **context}}


def test_essentials_are_never_blocked():
    for package in ("com.samsung.android.dialer", "com.samsung.android.messaging",
                    "com.sec.android.gallery3d", "com.samsung.android.calendar", "com.google.android.gm",
                    "com.sec.android.app.clockpackage", "dev.specter.phone"):
        assert package in DEFAULT_ALLOW
    for entertainment in ("tv.danmaku.bili", "com.twitter.android", "com.google.android.youtube",
                          "com.ss.android.ugc.trill", "com.xingin.xhs", "ai.x.grok"):
        assert entertainment not in DEFAULT_ALLOW


def test_configured_allow_list_always_keeps_specter(monkeypatch):
    monkeypatch.setenv("SPECTER_GUARD_ALLOW", "com.tencent.mm, com.whatsapp")
    monkeypatch.setenv("SPECTER_GUARD_ALLOW_EXTRA", "")
    assert allow_list() == ["com.tencent.mm", "com.whatsapp", "dev.specter.phone"]


def test_personal_extras_are_added_to_the_defaults(monkeypatch):
    monkeypatch.delenv("SPECTER_GUARD_ALLOW", raising=False)
    monkeypatch.setenv("SPECTER_GUARD_ALLOW_EXTRA", "com.tencent.mm, com.whatsapp, com.android.settings")
    packages = allow_list()
    assert packages[:len(DEFAULT_ALLOW)] == list(DEFAULT_ALLOW)
    assert packages[len(DEFAULT_ALLOW):] == ["com.tencent.mm", "com.whatsapp"]


def test_lockdown_lasts_until_the_next_morning():
    six = time(6, 0)
    assert next_morning(datetime(2026, 10, 19, 0, 52), six) == datetime(2026, 10, 19, 6, 0)
    assert next_morning(datetime(2026, 10, 18, 23, 10), six) == datetime(2026, 10, 19, 6, 0)
    payload = guard_payload("block", datetime(2026, 10, 19, 0, 52), six, ["a" * 32])
    assert payload["until_ms"] == int(datetime(2026, 10, 19, 6, 0).timestamp() * 1000)
    assert validate_guard(payload) == payload
    with pytest.raises(ValueError):
        guard_payload("forever", datetime(2026, 10, 19, 0, 52), six)


@pytest.mark.parametrize("bad", [
    {"mode": "block"},
    {"mode": "block", "until_ms": 5, "allow": []},
    {"mode": "block", "until_ms": 5, "allow": ["not a package"]},
    {"mode": "block", "until_ms": 5, "allow": ["com.tencent.mm"], "bounce_audio_ids": ["../x"]},
    {"mode": "explode"},
])
def test_invalid_guard_payloads_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_guard(bad)


def test_release_is_just_off():
    assert validate_guard({"mode": "off", "until_ms": 99}) == {"mode": "off"}


def test_strict_levels_have_their_own_buttons_and_lockdown():
    assert [a["id"] for a in phone_nudge_payload(strict_nudge(1))["actions"]] == ["accept", "snooze", "stay"]
    assert [a["id"] for a in phone_nudge_payload(strict_nudge(2))["actions"]] == ["accept", "stay"]
    guard = guard_payload("block", datetime(2026, 10, 19, 0, 52), time(6, 0))
    locked = phone_nudge_payload(strict_nudge(3, guard="block"), guard=guard)
    assert [a["id"] for a in locked["actions"]] == ["stay"]
    assert validate_nudge(locked)["guard"]["mode"] == "block"


def test_strict_text_names_the_app_and_the_broken_promise():
    request = phone_text_request(strict_nudge(3, caught="accept", app_label="bilibili", minutes_past_bedtime=5))
    assert request.startswith("They told you they were going to sleep")
    assert "bilibili" in request and "locking" in request
    assert "five minutes" in phone_text_request(strict_nudge(2))
    assert phone_fallback_text(strict_nudge(3, caught="accept")) == STRICT_CAUGHT_FALLBACK
    assert "locks itself" in phone_fallback_text(strict_nudge(4))


def test_bounce_reports_reach_the_callback_over_http():
    events = []
    link = PhoneLink(on_guard_event=events.append)
    server = make_server(link, "secret", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/guard/event",
            data=json.dumps({"event": "bounce", "package": "tv.danmaku.bili", "label": "bilibili"}).encode(),
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.load(response)["recorded"]["package"] == "tv.danmaku.bili"
        assert events == [{"event": "bounce", "package": "tv.danmaku.bili", "label": "bilibili"}]
        link.enqueue("guard", {"mode": "off"})
        assert link.next_command()["payload"] == {"mode": "off"}
    finally:
        server.shutdown()
