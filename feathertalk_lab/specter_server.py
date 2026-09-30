"""Specter PCM bridge and lab preview, sharing one trained FeatherTalk engine."""
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
from idle_transition import IdleTransition
from streaming_resampler import StreamingResampler24To16
from loop_server import engine, lock, serve

MODE = os.getenv("FEATHERTALK_MODE", "fast").strip().lower()
TEETH = os.getenv("FEATHERTALK_TEETH_MODE", "new").strip().lower()
BUFFER_MS = int(os.getenv("FEATHERTALK_BUFFER_MS", "100"))
if MODE not in {"fast", "balanced", "quality"}:
    raise ValueError("SPECTER_FEATHERTALK_MODE must be fast, balanced or quality")
if TEETH not in {"model", "new"} or (TEETH == "new" and engine.teeth is None):
    raise ValueError("SPECTER_FEATHERTALK_TEETH_MODE must be model or available new teeth")
if not 40 <= BUFFER_MS <= 1000:
    raise ValueError("SPECTER_FEATHERTALK_BUFFER_MS must be between 40 and 1000")
BUFFER_BYTES = 24000 * BUFFER_MS // 1000 * 2
SETTLE_MS = int(os.getenv("FEATHERTALK_SETTLE_MS", "320"))
if not 80 <= SETTLE_MS <= 1000:
    raise ValueError("SPECTER_FEATHERTALK_SETTLE_MS must be between 80 and 1000")
SETTLE_FRAMES = max(2, round(SETTLE_MS / 40))

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


@app.get("/")
def home():
    return FileResponse("/lab/specter.html", headers={"Cache-Control": "no-store"})


@app.get("/avatar/video")
def video():
    return FileResponse(engine.idle_video_path, media_type="video/mp4", headers={"Cache-Control": "no-cache"})


@app.get("/poster")
def poster():
    return FileResponse(engine.idle_poster_path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    return {"ok": True, "model": "FeatherTalk loop", "publisher": publisher is not None,
            "viewers": len(viewers), "speech_active": active, "mode": MODE,
            "buffer_ms": BUFFER_MS, "teeth_mode": TEETH,
            "checkpoint": "retrain_20260929/best.pth", "base_frames": engine.count,
            "frames_rendered": frames_rendered, "teeth_restored_frames": teeth_restored_frames,
            "settle_ms": SETTLE_FRAMES * 40, "settle_frames_rendered": settle_frames_rendered,
            "detail_profile": engine.detail_profile, "detail_last_ms": engine.detail_last_ms,
            "error": last_error}


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
    global frames_rendered, teeth_restored_frames
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    images = []
    restored_before = session.teeth_restored_frames
    for index, image in session.push(pcm, final):
        ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise RuntimeError("FeatherTalk JPEG encoding failed")
        images.append((index, jpeg.tobytes()))
        frames_rendered += 1
    teeth_restored_frames += session.teeth_restored_frames - restored_before
    return images


def infer_settle(transition):
    global settle_frames_rendered
    index, image = transition.next_frame()
    ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise RuntimeError("FeatherTalk transition JPEG encoding failed")
    settle_frames_rendered += 1
    return index, jpeg.tobytes()


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
    settling = None
    settle_next_at = 0.

    async def release(control="idle"):
        nonlocal session, owned, last_audio, settling
        global active, latest
        session = None
        settling = None
        pending.clear()
        resampler.reset()
        last_audio = 0.
        active = False
        latest = None
        if owned:
            lock.release()
            owned = False
        await broadcast({"type": control})

    async def begin_settle(interrupted=False):
        nonlocal session, settling, settle_next_at
        source = settling if settling is not None else session
        pending.clear()
        resampler.reset()
        if source is None or source.last_image is None:
            await release("reset")
            return
        settling = IdleTransition(engine, source.last_index, source.last_image, SETTLE_FRAMES)
        session = None
        settle_next_at = time.monotonic()
        await broadcast({"type": "settling", "drop_pending": interrupted})

    async def step_settle():
        nonlocal settle_next_at
        global latest
        started = time.monotonic()
        index, image = await asyncio.to_thread(infer_settle, settling)
        latest = (index, image)
        await broadcast({"type": "frame", "index": index, "phase": "settling",
                         "step": settling.step, "steps": settling.frames}, image)
        settle_next_at = started + .04
        if settling.step >= settling.frames:
            await release()

    async def render(raw, final=False):
        global latest
        images = await asyncio.to_thread(infer, session, raw, final)
        for index, image in images:
            latest = (index, image)
            await broadcast({"type": "frame", "index": index}, image)

    try:
        await broadcast({"type": "reset"})
        while True:
            try:
                timeout = max(.001, settle_next_at - time.monotonic()) if settling is not None else .1
                event = await asyncio.wait_for(ws.receive(), timeout=timeout)
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
                    if owned:
                        # Resume from the moving tail, without seeking back to
                        # the paused video's old frame or hiding the face.
                        settling = None
                        requested_frame = engine.cursor
                        await broadcast({"type": "resume"})
                    else:
                        if not lock.acquire(blocking=False):
                            raise RuntimeError("FeatherTalk lab preview is busy; stop its sample playback first")
                        owned = True
                        active = True
                        latest = None
                        requested_frame = None
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
            if session is not None and time.monotonic() - last_audio > .24:
                await render(resampler.finish(), final=True)
                await begin_settle()
            if settling is not None and time.monotonic() >= settle_next_at:
                await step_settle()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"
        print("FeatherTalk Realtime bridge failed:", last_error, flush=True)
        await broadcast({"type": "error", "message": last_error})
        await ws.close(code=1011)
    finally:
        # Session shutdown closes the audio publisher; finish the visible tail
        # as well, so an ordinary end-conversation does not abruptly cut to idle.
        try:
            if active:
                await begin_settle(interrupted=True)
                while settling is not None:
                    await asyncio.sleep(max(0., settle_next_at - time.monotonic()))
                    await step_settle()
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
    viewers[ws] = asyncio.Lock()
    try:
        if active:
            await send(ws, [{"type": "start"}])
            if latest is not None:
                await send(ws, [{"type": "frame", "index": latest[0]}, latest[1]])
        while True:
            event = await ws.receive()
            if event["type"] == "websocket.disconnect":
                break
            if event.get("text"):
                control = json.loads(event["text"])
                if control.get("type") == "sync" and isinstance(control.get("frame"), int):
                    requested_frame = max(0, control["frame"]) % engine.count
                    sync_event.set()
    except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
        pass
    finally:
        viewers.pop(ws, None)


if __name__ == "__main__":
    import uvicorn
    # EN: The original experiment keeps port 8080 and shares its GPU/model lock.
    # 中文：原实验页继续使用 8080，与实时桥接共享同一份 GPU 模型和互斥锁。
    threading.Thread(target=serve, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8081, log_level="info")
