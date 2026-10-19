"""Phone notification channel: protocol, payload, and text constraints (no device)."""

import json
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

import phone_link
from nudge_prompt import (
    PHONE_FALLBACK_TEXT, clean_phone_text, phone_nudge_payload, phone_text_request,
)
from phone_link import PhoneLink, make_server, validate_nudge

NUDGE = {
    "id": "n1", "rule": "bedtime", "level": 2, "channel": "phone",
    "context": {"minutes_past_bedtime": 25, "phone_app": "com.twitter.android", "evidence": ["phone"]},
}


def test_payload_has_title_fallback_text_and_three_buttons():
    payload = phone_nudge_payload(NUDGE)
    assert payload["title"] == "Specter"
    assert payload["text"] == PHONE_FALLBACK_TEXT[2]
    assert [a["id"] for a in payload["actions"]] == ["accept", "snooze", "stay"]
    assert validate_nudge(payload) == payload
    assert phone_nudge_payload(NUDGE, "Bed. Now.")["text"] == "Bed. Now."


def test_text_request_keeps_app_names_local():
    request = phone_text_request(NUDGE)
    assert "25 minutes" in request and "second bedtime reminder" in request
    assert "twitter" not in request.lower()


@pytest.mark.parametrize("raw, expected", [
    ('"Phone down. Sleep."', "Phone down. Sleep."),
    ("  Lights off,\n  now. ", "Lights off, now."),
    ("", None),
    ("x" * 300, None),
])
def test_model_text_is_cleaned_or_rejected(raw, expected):
    assert clean_phone_text(raw) == expected


@pytest.mark.parametrize("bad", [
    {"title": "", "text": "t", "nudge_id": "n"},
    {"title": "Specter", "text": "t" * 401, "nudge_id": "n"},
    {"title": "Specter", "text": "t", "nudge_id": "n", "actions": [{"id": "dance", "label": "x"}]},
    {"title": "Specter", "text": "t", "nudge_id": "n", "actions": [{"id": "snooze", "label": "x"}]},
    {"title": "Specter", "text": "t", "nudge_id": "n", "actions": [{"id": "accept", "label": "a"}] * 4},
    "not an object",
])
def test_invalid_nudge_payloads_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_nudge(bad)


def test_focus_commands_take_no_payload():
    with pytest.raises(ValueError):
        PhoneLink().enqueue("focus_on", {"title": "x"})


def test_ack_and_expiry_callbacks_report_the_nudge():
    acked, expired = [], []
    link = PhoneLink(on_ack=acked.append, on_expired=expired.append)
    shown = link.enqueue("nudge", phone_nudge_payload(NUDGE))
    assert link.next_command()["payload"]["nudge_id"] == "n1"
    assert link.ack(shown.id)
    assert [c.payload["nudge_id"] for c in acked] == ["n1"]
    missed = link.enqueue("nudge", phone_nudge_payload({**NUDGE, "id": "n2"}))
    with patch.object(phone_link.time, "time", return_value=missed.created_at + 121):
        link.expire_commands()
    assert [c.payload["nudge_id"] for c in expired] == ["n2"]
    assert link.next_command() is None


def test_http_round_trip_for_nudge_and_button_reply():
    replies = []
    link = PhoneLink(on_nudge_response=replies.append)
    server = make_server(link, "test-secret", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path, value=None):
        request = urllib.request.Request(
            base + path, data=json.dumps(value).encode() if value is not None else None,
            headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    try:
        call("/v1/commands", {"name": "nudge", "payload": phone_nudge_payload(NUDGE)})
        command = call("/v1/commands/next")["command"]
        assert command["name"] == "nudge"
        assert command["payload"]["actions"][1]["snooze_minutes"] == 10
        assert call("/v1/commands/ack", {"id": command["id"]}) == {"acknowledged": True}
        call("/v1/nudges/response", {"nudge_id": "n1", "action": "snooze", "snooze_minutes": 10})
        assert replies == [{"nudge_id": "n1", "action": "snooze", "snooze_minutes": 10}]
        with pytest.raises(urllib.error.HTTPError) as error:
            call("/v1/nudges/response", {"nudge_id": "n1", "action": "snooze"})
        assert error.value.code == 400
    finally:
        server.shutdown()


def test_audio_store_is_content_addressed_and_bounded():
    link = PhoneLink()
    first = link.store_audio(b"clip-one")
    assert link.store_audio(b"clip-one") == first
    assert link.get_audio(first) == b"clip-one"
    for index in range(phone_link.MAX_STORED_AUDIO):
        link.store_audio(f"clip-{index}".encode())
    assert link.get_audio(first) is None  # oldest evicted
    with pytest.raises(ValueError):
        link.store_audio(b"")


def test_payload_carries_voice_ids_and_rejects_unknown_ones():
    link = PhoneLink()
    voice, stay = link.store_audio(b"voice"), link.store_audio(b"stay")
    payload = phone_nudge_payload(NUDGE, "Bed.", voice, stay)
    assert validate_nudge(payload)["audio_id"] == voice
    assert validate_nudge(payload)["stay_audio_id"] == stay
    with pytest.raises(ValueError):
        validate_nudge({**payload, "audio_id": "../etc/passwd"})


def test_http_audio_upload_download_and_stay_reply():
    replies = []
    link = PhoneLink(on_nudge_response=replies.append)
    server = make_server(link, "test-secret", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def raw(path, data=None, content_type="application/json", token="test-secret"):
        request = urllib.request.Request(base + path, data=data, headers={
            "Authorization": f"Bearer {token}", "Content-Type": content_type,
        })
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.read(), response.headers.get("Content-Type")

    try:
        body, _ = raw("/v1/audio", b"ID3fake-mp3", "audio/mpeg")
        audio_id = json.loads(body)["audio_id"]
        data, kind = raw(f"/v1/audio/{audio_id}")
        assert (data, kind) == (b"ID3fake-mp3", "audio/mpeg")
        with pytest.raises(urllib.error.HTTPError) as missing:
            raw("/v1/audio/" + "0" * 32)
        assert missing.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as denied:
            raw(f"/v1/audio/{audio_id}", token="wrong")
        assert denied.value.code == 401
        raw("/v1/nudges/response", json.dumps({"nudge_id": "n1", "action": "stay"}).encode())
        assert replies == [{"nudge_id": "n1", "action": "stay"}]
    finally:
        server.shutdown()


def test_stay_audio_is_recorded_once_and_reused(tmp_path, monkeypatch):
    import nudge_voice

    calls = []
    monkeypatch.setattr(nudge_voice, "synthesize", lambda text, instructions, timeout: calls.append(text) or b"mp3")
    assert nudge_voice.stay_audio(tmp_path) == b"mp3"
    assert nudge_voice.stay_audio(tmp_path) == b"mp3"
    assert len(calls) == 1 and "I'm here" in calls[0]
    monkeypatch.setenv("SPECTER_NUDGE_TTS_VOICE", "onyx")
    nudge_voice.stay_audio(tmp_path)
    assert len(calls) == 2  # a different voice is a new recording


def test_tired_day_adds_only_a_coarse_hint():
    tired = {**NUDGE, "context": {**NUDGE["context"], "foci_label": "stressed"}}
    request = phone_text_request(tired)
    assert "draining" in request and "stressed" not in request
