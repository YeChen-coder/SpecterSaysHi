"""Nonblocking copy of played 24 kHz PCM for the selected avatar display."""

import asyncio
import logging
import os
import struct
import time

LOG = logging.getLogger(__name__)

AVATAR_ENV_KEYS = {
    "SPECTER_AVATAR_ENABLED", "SPECTER_AVATAR_BACKEND",
    "SPECTER_DH_LIVE_ENABLED", "SPECTER_DH_LIVE_MODEL", "SPECTER_DH_LIVE_BUFFER_MS",
    "SPECTER_FEATHERTALK_WS_URL", "SPECTER_FEATHERTALK_MODE",
    "SPECTER_FEATHERTALK_BUFFER_MS", "SPECTER_FEATHERTALK_TEETH_MODE",
    "SPECTER_FEATHERTALK_SETTLE_MS",
    "SPECTER_FEATHERTALK_DETAIL_PROFILE",
}


def avatar_enabled() -> bool:
    return os.getenv("SPECTER_AVATAR_ENABLED", os.getenv(
        "SPECTER_DH_LIVE_ENABLED", os.getenv("SPECTER_MINI_ENABLED", "false")
    )).strip().lower() in {"1", "true", "yes", "on"}


class AvatarAudioPublisher:
    def __init__(self, url: str | None = None):
        self.backend = os.getenv("SPECTER_AVATAR_BACKEND", "dh_live").strip().lower()
        if self.backend == "feathertalk":
            selected = "loop"
            default = os.getenv("SPECTER_FEATHERTALK_WS_URL", "ws://127.0.0.1:18988/publish")
        elif self.backend == "dh_live":
            selected = os.getenv("SPECTER_DH_LIVE_MODEL", "mini").strip().lower()
            if selected not in {"mini", "full"}:
                raise ValueError("SPECTER_DH_LIVE_MODEL must be mini or full")
            default = ("ws://127.0.0.1:18890/publish" if selected == "full"
                       else os.getenv("SPECTER_MINI_WS_URL", "ws://127.0.0.1:18888/publish"))
        else:
            raise ValueError("SPECTER_AVATAR_BACKEND must be dh_live or feathertalk")
        self.url = url or default
        self.model = selected
        self.queue: asyncio.Queue[bytes | str] = asyncio.Queue(maxsize=16)

    def reset(self):
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait('{"type":"reset"}')

    def push(self, pcm: bytes):
        if len(pcm) % 2:
            return
        if self.queue.full():
            self.reset()
        self.queue.put_nowait(struct.pack("<d", time.time() * 1000) + pcm)

    async def run(self):
        import websockets
        while True:
            try:
                async with websockets.connect(self.url, open_timeout=2, max_size=None) as ws:
                    LOG.info("Avatar %s %s audio relay connected", self.backend, self.model)
                    await ws.send('{"type":"reset"}')
                    while True:
                        await ws.send(await self.queue.get())
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                LOG.warning("Avatar %s %s audio relay disconnected: %s", self.backend, self.model, exc)
                self.reset()
                await asyncio.sleep(2)


# EN: Keep the old import for existing DH_live smoke scripts.
# 中文：保留旧类名，兼容现有 DH_live 冒烟测试脚本。
MiniAudioPublisher = AvatarAudioPublisher
