"""Backend routing and nonblocking played-audio transport regression checks."""
import asyncio
import json
import struct

import pytest

from mini_audio_publisher import AvatarAudioPublisher, MiniAudioPublisher, avatar_enabled, AVATAR_ENV_KEYS


@pytest.fixture(autouse=True)
def clean_avatar_environment(monkeypatch):
    for name in AVATAR_ENV_KEYS | {"SPECTER_MINI_ENABLED", "SPECTER_MINI_WS_URL"}:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("backend,model,url", [
    ("dh_live", "mini", "ws://127.0.0.1:18888/publish"),
    ("dh_live", "full", "ws://127.0.0.1:18890/publish"),
    ("feathertalk", "full", "ws://127.0.0.1:18988/publish"),
])
def test_routes_only_to_selected_backend(monkeypatch, backend, model, url):
    monkeypatch.setenv("SPECTER_AVATAR_BACKEND", backend)
    monkeypatch.setenv("SPECTER_DH_LIVE_MODEL", model)
    assert AvatarAudioPublisher().url == url
    assert MiniAudioPublisher is AvatarAudioPublisher


def test_new_enable_flag_overrides_legacy_flags(monkeypatch):
    assert not avatar_enabled()
    monkeypatch.setenv("SPECTER_MINI_ENABLED", "true")
    assert avatar_enabled()
    monkeypatch.setenv("SPECTER_DH_LIVE_ENABLED", "false")
    assert not avatar_enabled()
    monkeypatch.setenv("SPECTER_AVATAR_ENABLED", "true")
    assert avatar_enabled()
    monkeypatch.setenv("SPECTER_AVATAR_ENABLED", "false")
    assert not avatar_enabled()


def test_feather_url_does_not_use_dh_model_or_mini_url(monkeypatch):
    monkeypatch.setenv("SPECTER_AVATAR_BACKEND", "feathertalk")
    monkeypatch.setenv("SPECTER_DH_LIVE_MODEL", "unused")
    monkeypatch.setenv("SPECTER_MINI_WS_URL", "ws://wrong/publish")
    monkeypatch.setenv("SPECTER_FEATHERTALK_WS_URL", "ws://custom/publish")
    assert AvatarAudioPublisher().url == "ws://custom/publish"
    monkeypatch.setenv("SPECTER_AVATAR_BACKEND", "unknown")
    with pytest.raises(ValueError, match="SPECTER_AVATAR_BACKEND"):
        AvatarAudioPublisher()


def test_interruption_and_overflow_discard_stale_pcm():
    publisher = AvatarAudioPublisher()
    pcm = b"\x01\x00" * 240
    publisher.push(pcm)
    packet = publisher.queue.get_nowait()
    assert struct.unpack("<d", packet[:8])[0] > 0
    assert packet[8:] == pcm
    publisher.push(pcm)
    publisher.reset()
    assert publisher.queue.qsize() == 1
    assert json.loads(publisher.queue.get_nowait()) == {"type": "reset"}
    for _ in range(17):
        publisher.push(pcm)
    assert publisher.queue.qsize() == 2
    assert json.loads(publisher.queue.get_nowait()) == {"type": "reset"}
    assert publisher.queue.get_nowait()[8:] == pcm
    publisher.push(b"\x00")
    assert publisher.queue.empty()


def test_publisher_connects_and_sends_played_pcm(monkeypatch):
    import websockets

    async def check():
        received = []
        arrived = asyncio.Event()

        async def handler(ws):
            async for packet in ws:
                received.append(packet)
                if isinstance(packet, bytes):
                    arrived.set()

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setenv("SPECTER_AVATAR_BACKEND", "feathertalk")
            publisher = AvatarAudioPublisher(f"ws://127.0.0.1:{port}")
            task = asyncio.create_task(publisher.run())
            try:
                publisher.push(b"\x04\x00" * 240)
                await asyncio.wait_for(arrived.wait(), 3)
                assert json.loads(received[0]) == {"type": "reset"}
                assert received[-1][8:] == b"\x04\x00" * 240
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(check())
