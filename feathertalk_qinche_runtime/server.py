"""Independent QinChe PCM bridge; approved native-face composition only."""
import asyncio
import json
import os
import threading
import time

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from realtime_session import RealtimeLoopSession
from streaming_resampler import StreamingResampler24To16
from frame_stream import iter_frames
from frame_encoding import encode_frame, frame_metadata
from engine import QinCheEngine

engine = QinCheEngine()
lock = threading.Lock()

MODE = os.getenv("QINCHE_MODE", "fast").strip().lower()
TEETH = "model"
BUFFER_MS = int(os.getenv("QINCHE_BUFFER_MS", "100"))
if MODE not in {"fast", "balanced", "quality"}:
    raise ValueError("SPECTER_QINCHE_MODE must be fast, balanced or quality")
if not 40 <= BUFFER_MS <= 1000:
    raise ValueError("SPECTER_QINCHE_BUFFER_MS must be between 40 and 1000")
BUFFER_BYTES = 24000 * BUFFER_MS // 1000 * 2
SETTLE_FRAMES = 0

app = FastAPI()
viewers = {}
publisher = None
active = False
latest = None
requested_frame = None
sync_event = asyncio.Event()
frames_rendered = 0
teeth_restored_frames = 0
settle_frames_rendered = 0
last_error = ""
peak_pcm_samples = 0
current_pcm_samples = 0
timeline_anchor = 0
timeline_anchor_at = time.monotonic()


@app.get("/")
def home():
    return FileResponse("/runtime/specter.html", headers={"Cache-Control": "no-store"})


@app.get("/avatar/video")
def video():
    return FileResponse(engine.idle_video_path, media_type="video/mp4", headers={"Cache-Control": "no-cache"})


@app.get("/preview")
def preview():
    return home()

@app.get("/character-test.wav")
def character_test_audio():
    return FileResponse('/assets/data/test_speech.wav', media_type='audio/wav')

@app.get("/poster")
def poster():
    return FileResponse(engine.idle_poster_path, media_type="image/jpeg")


@app.get("/health")
def health():
    return {**engine.health(), "ok": True, "publisher": publisher is not None,
            "viewers": len(viewers), "speech_active": active, "mode": MODE,
            "buffer_ms": BUFFER_MS, "extra_future_audio_ms": {"fast":0,"balanced":400,"quality":1000}[MODE],
            "audio_input_hz":24000,"audio_model_hz":16000,"packet_header_bytes":8,
            "resampler_lookahead_ms":32/24,
            "frames_rendered":frames_rendered,"settle_ms":0,"settle_frames_rendered":0,
            "history_limit_ms":4000,"peak_audio_history_ms":peak_pcm_samples/16,
            "current_audio_history_ms":current_pcm_samples/16,"frame_delivery":"incremental",
            "error":last_error}



async def send(ws, messages):
    async with viewers[ws]:
        for item in messages:
            if isinstance(item, bytes):
                await ws.send_bytes(item)
            else:
                await ws.send_text(json.dumps(item))


async def broadcast(*messages):
    for ws in tuple(viewers):
        try:
            await asyncio.wait_for(send(ws, messages), timeout=.2)
        except (OSError, RuntimeError, asyncio.TimeoutError, KeyError, WebSocketDisconnect):
            viewers.pop(ws, None)
            try:
                await ws.close()
            except (OSError, RuntimeError):
                pass


def infer(session, raw, final=False):
    global frames_rendered, teeth_restored_frames, peak_pcm_samples, current_pcm_samples
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    restored_before = session.teeth_restored_frames
    for index, image in session.push(pcm, final):
        encoded = encode_frame(engine,index,image)
        frames_rendered += 1
        yield index, encoded
    teeth_restored_frames += session.teeth_restored_frames - restored_before
    peak_pcm_samples = max(peak_pcm_samples, session.peak_pcm_samples)
    current_pcm_samples = len(session.pcm)


@app.websocket("/publish")
async def publish(ws: WebSocket):
    global publisher, active, latest, requested_frame, last_error
    await ws.accept()
    if publisher is not None:
        await ws.close(code=1013, reason="publisher already connected")
        return
    publisher = ws
    session = None
    owned = False
    pending = bytearray()
    resampler = StreamingResampler24To16()
    last_audio = 0.

    async def release(control="idle"):
        nonlocal session, owned, last_audio
        global active, latest, timeline_anchor, timeline_anchor_at, current_pcm_samples
        if active:
            timeline_anchor = engine.cursor
            timeline_anchor_at = time.monotonic()
        session = None
        pending.clear()
        resampler.reset()
        last_audio = 0.
        active = False
        latest = None
        current_pcm_samples = 0
        if owned:
            lock.release()
            owned = False
        await broadcast({"type": control})

    async def begin_settle(interrupted=False):
        # Approved clips return directly to native idle; no Gentleman mouth tail.
        await release("reset" if interrupted else "idle")

    async def render(raw, final=False):
        global latest
        async for index, image in iter_frames(infer(session, raw, final)):
            latest = (index, image)
            await broadcast(frame_metadata(engine,index), image)

    try:
        await broadcast({"type": "reset"})
        while True:
            try:
                event = await asyncio.wait_for(ws.receive(), timeout=.1)
            except asyncio.TimeoutError:
                event = None
            if event and event["type"] == "websocket.disconnect":
                break
            if event and event.get("text"):
                if json.loads(event["text"]).get("type") == "reset":
                    if active:
                        await begin_settle(interrupted=True)
                    else:
                        await release("reset")
            if event and event.get("bytes"):
                packet = event["bytes"]
                if len(packet) <= 8 or (len(packet) - 8) % 2 or len(packet) > 96008:
                    raise ValueError("Expected timestamp + mono 24 kHz PCM16 packet (maximum 2 seconds)")
                if session is None:
                    if not lock.acquire(blocking=False):
                        raise RuntimeError("QinChe engine is busy")
                    owned = True
                    active = True
                    latest = None
                    # Keep advancing the idle timeline even without a browser.
                    # A connected viewer can replace this with its exact video frame.
                    requested_frame = (timeline_anchor + int((time.monotonic()-timeline_anchor_at)*25)) % engine.count
                    sync_event.clear()
                    await broadcast({"type": "start"})
                    if viewers:
                        try:
                            await asyncio.wait_for(sync_event.wait(), timeout=.1)
                        except asyncio.TimeoutError:
                            pass
                    session = await asyncio.to_thread(
                        RealtimeLoopSession, engine, MODE, requested_frame or 0, TEETH)
                    last_error = ""
                pending.extend(packet[8:])
                last_audio = time.monotonic()
            # EN: Flush a short final packet on timeout; continuous packets use the chosen buffer.
            # 中文：连续音频按设定缓冲推理；超时时排空不足一包的尾音。
            if session is not None and pending and (len(pending) >= BUFFER_BYTES or event is None):
                converted = resampler.push_pcm(bytes(pending))
                pending.clear()
                await render(converted)
            # Only end speech after a receive timeout. A slow inference packet
            # can take >240 ms while subsequent audio is already queued in the
            # socket; processing time must not split that continuous speech.
            if session is not None and event is None and time.monotonic() - last_audio > .24:
                await render(resampler.finish(), final=True)
                await begin_settle()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"
        print("FeatherTalk Realtime bridge failed:", last_error, flush=True)
        await broadcast({"type": "error", "message": last_error})
        await ws.close(code=1011)
    finally:
        # Disconnects invalidate pending browser frames and return to native idle.
        try:
            if active:
                await begin_settle(interrupted=True)
        finally:
            try:
                if owned:
                    await release("reset")
            finally:
                publisher = None


@app.websocket("/view")
async def view(ws: WebSocket):
    global requested_frame
    await ws.accept()
    if ws.query_params.get('protocol')!='2':
        await ws.close(code=1008,reason='Refresh viewer for mouth-only transport')
        return
    viewers[ws] = asyncio.Lock()
    try:
        if active:
            await send(ws, [{"type": "start"}])
            if latest is not None:
                await send(ws, [frame_metadata(engine,latest[0]), latest[1]])
        while True:
            event = await ws.receive()
            if event["type"] == "websocket.disconnect":
                break
            if event.get("text"):
                control = json.loads(event["text"])
                if control.get("type") == "sync" and isinstance(control.get("frame"), int) and not sync_event.is_set():
                    requested_frame = max(0, control["frame"]) % engine.count
                    sync_event.set()
    except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
        pass
    finally:
        viewers.pop(ws, None)


if __name__ == "__main__":
    import uvicorn
    # The former preview port redirects to the same production UI. Only one
    # asyncio loop owns the publisher, viewer locks and synchronization event.
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class PreviewRedirect(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                body = json.dumps({**health(), "ready":True}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(307)
                self.send_header('Location','http://127.0.0.1:18988/')
                self.send_header('Content-Length','0')
                self.end_headers()
    threading.Thread(target=ThreadingHTTPServer(('0.0.0.0',8080),PreviewRedirect).serve_forever,
                     daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8081, log_level="info", ws_per_message_deflate=False)
