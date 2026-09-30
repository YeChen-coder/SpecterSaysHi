"""Loopback-only DH_live_mini static server and live PCM relay."""

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

app = FastAPI()
viewers: set[WebSocket] = set()
publisher: WebSocket | None = None
bytes_relayed = 0
inference_url = os.getenv("DH_LIVE_INFERENCE_URL", "http://dh-live-inference:8899")
buffer_ms = int(os.getenv("DH_LIVE_AUDIO_BUFFER_MS", "100"))
if not 40 <= buffer_ms <= 1000:
    raise ValueError("DH_LIVE_AUDIO_BUFFER_MS must be between 40 and 1000")


@app.get("/")
def home():
    return RedirectResponse("/static/SpecterMini.html")


@app.get("/health")
def health():
    return {"ok": True, "publisher": publisher is not None,
            "viewers": len(viewers), "bytes_relayed": bytes_relayed,
            "buffer_ms": buffer_ms}


@app.get("/settings")
def settings():
    return {"buffer_ms": buffer_ms}


@app.post("/inference/{operation}")
async def inference(operation: str, request: Request):
    routes = {
        "render": "/render",
        "audio-push": "/audio/push",
        "audio-reset": "/audio/reset",
    }
    if operation not in routes:
        return Response(status_code=404)
    body = await request.body()
    if len(body) > 184 * 184 * 8:
        return Response(status_code=413)
    session = request.headers.get("X-Specter-Session", "default")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session):
        return Response(status_code=400)

    def forward():
        upstream = urllib.request.Request(
            inference_url + routes[operation], data=body, method="POST",
            headers={"Content-Type": "application/octet-stream",
                     "X-Specter-Session": session},
        )
        with urllib.request.urlopen(upstream, timeout=3) as result:
            return result.status, result.headers.get("Content-Type"), result.read()

    try:
        status, content_type, result = await asyncio.to_thread(forward)
        return Response(result, status_code=status,
                        media_type=content_type or "application/octet-stream")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"inference {operation} unavailable: {exc}", flush=True)
        return Response("inference temporarily unavailable", status_code=503,
                        media_type="text/plain")


async def broadcast(data: bytes | str):
    failed = []
    for viewer in tuple(viewers):
        try:
            if isinstance(data, bytes):
                await asyncio.wait_for(viewer.send_bytes(data), timeout=0.2)
            else:
                await asyncio.wait_for(viewer.send_text(data), timeout=0.2)
        except (OSError, RuntimeError, asyncio.TimeoutError):
            failed.append(viewer)
    for viewer in failed:
        viewers.discard(viewer)
        await viewer.close()


@app.websocket("/publish")
async def publish(ws: WebSocket):
    global publisher, bytes_relayed
    await ws.accept()
    if publisher is not None:
        await ws.close(code=1013, reason="publisher already connected")
        return
    publisher = ws
    await broadcast('{"type":"reset"}')
    try:
        while True:
            event = await ws.receive()
            if event.get("type") == "websocket.disconnect":
                break
            chunk = event.get("bytes")
            if chunk is not None:
                if 8 < len(chunk) <= 48008:
                    bytes_relayed += len(chunk) - 8
                    await broadcast(chunk)
            else:
                message = event.get("text")
                if message:
                    try:
                        control = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    if control.get("type") == "reset":
                        await broadcast('{"type":"reset"}')
    finally:
        if publisher is ws:
            publisher = None
            await broadcast('{"type":"reset"}')


@app.websocket("/view")
async def view(ws: WebSocket):
    await ws.accept()
    viewers.add(ws)
    try:
        while True:
            await ws.receive()
    except WebSocketDisconnect:
        pass
    finally:
        viewers.discard(ws)


class FreshStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


static_dir = Path(os.getenv("DH_LIVE_STATIC_DIR", "/opt/DH_live/web_demo/static"))
app.mount("/static", FreshStaticFiles(directory=static_dir), name="static")
