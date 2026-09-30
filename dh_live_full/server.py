"""Realtime bridge for the original five-reference DH_live model."""

import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import torch
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from streaming_resampler import StreamingResampler24To16
from avatar_references import apply_appearance_references
from teeth_restore import TeethRestorer
from idle_animation import rest_mouth, settle_mouth
from mouth_clarity import MouthClarity


SOURCE_RATE = 24000
FRAME_MS = 40
MODEL_FRAME_BYTES = 16000 * FRAME_MS // 1000 * 2
buffer_ms = int(os.getenv("DH_LIVE_AUDIO_BUFFER_MS", "100"))
if not 40 <= buffer_ms <= 1000:
    raise ValueError("DH_LIVE_AUDIO_BUFFER_MS must be between 40 and 1000")
BUFFER_BYTES = SOURCE_RATE * buffer_ms // 1000 * 2
settle_ms = int(os.getenv('DH_LIVE_SETTLE_MS', '320'))
if not 80 <= settle_ms <= 1000:
    raise ValueError('DH_LIVE_SETTLE_MS must be between 80 and 1000')
SETTLE_FRAMES = max(2, round(settle_ms / FRAME_MS))
teeth_mode = os.getenv('DH_LIVE_TEETH_MODE', 'source')
teeth_strength = float(os.getenv('DH_LIVE_TEETH_STRENGTH', '.85'))
jpeg_quality = int(os.getenv('DH_LIVE_JPEG_QUALITY', '95'))
if not 1 <= jpeg_quality <= 100:
    raise ValueError('DH_LIVE_JPEG_QUALITY must be between 1 and 100')

app = FastAPI()
viewers: set[WebSocket] = set()
publisher: WebSocket | None = None
model = None
render = None
frames_rendered = 0
last_render_ms = 0.0
last_error = ""
latest_frame: bytes | None = None
latest_frame_index: int | None = None
speech_active = False
requested_frame: int | None = None
sync_event = asyncio.Event()
avatar_config = {'name': 'legacy-reference', 'reference_indices': [0, 75, 150, 225, 300]}
avatar_frame_count = 0
teeth = None
idle_video_path = Path('/avatar/circle.mp4')
idle_mouth = None
last_mouth = None
settle_frames_rendered = 0
clarity = None


@app.on_event("startup")
async def load_models():
    global model, render, last_error, avatar_config, avatar_frame_count, teeth
    global idle_video_path, idle_mouth
    global clarity
    try:
        config_path = Path('/avatar/avatar_config.json')
        if config_path.is_file():
            avatar_config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        indices = avatar_config.get('render_reference_indices', avatar_config['reference_indices'])
        if len(indices) != 5 or len(set(indices)) != 5:
            raise ValueError('avatar requires five distinct reference frame indices')
        def load():
            from talkingface.audio_model import AudioModel
            from talkingface.render_model import RenderModel

            torch.set_num_threads(2)
            audio = AudioModel()
            audio.loadModel("/checkpoint/audio.pkl")
            video = RenderModel()
            video.loadModel("/checkpoint/render.pth")
            capture = cv2.VideoCapture('/avatar/circle.mp4')
            count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            capture.release()
            if count <= 0 or any(not isinstance(index, int) or index < 0 or index >= count
                                 for index in indices):
                raise ValueError('avatar reference frame indices are outside its video')
            video.reset_charactor("/avatar/circle.mp4", "/avatar/keypoint_rotate.pkl",
                                  indices)
            apply_appearance_references(video, '/avatar', avatar_config)
            tooth_restorer = TeethRestorer('/avatar', avatar_config, teeth_mode, teeth_strength)
            mouth_clarity = MouthClarity('/avatar', avatar_config)
            if avatar_config.get('idle_video'):
                cached_strength = float(avatar_config.get('idle_render', {}).get('mouth_clarity_strength', 0.))
                if cached_strength != mouth_clarity.strength:
                    raise ValueError('rebuild idle cache to match mouth clarity strength')
            with torch.inference_mode():
                resting = rest_mouth(audio)
            if avatar_config.get('idle_mouth'):
                filename = avatar_config['idle_mouth']
                if Path(filename).name != filename:
                    raise ValueError('idle_mouth must be a filename inside the avatar')
                resting = np.load(Path('/avatar')/filename, allow_pickle=False)
                if resting.shape != (15,30,3) or resting.dtype != np.uint8:
                    raise ValueError('idle mouth must be a uint8 15x30 RGB pose')
            return audio, video, count, tooth_restorer, resting, mouth_clarity

        model, render, avatar_frame_count, teeth, idle_mouth, clarity = await asyncio.to_thread(load)
        if avatar_config.get('idle_video'):
            filename = avatar_config['idle_video']
            if Path(filename).name != filename:
                raise ValueError('idle_video must be a filename inside the avatar')
            idle_video_path = Path('/avatar')/filename
            capture = cv2.VideoCapture(str(idle_video_path))
            count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            capture.release()
            if count != avatar_frame_count:
                raise ValueError('rendered idle must match the reference video frame count')
        print(f"Full DH_live avatar loaded: {avatar_config['name']}, reference frames {avatar_config['reference_indices']}", flush=True)
    except Exception as exc:
        model, render = None, None
        last_error = f"{type(exc).__name__}: {exc}"
        print(f"Full DH_live startup failed: {last_error}", flush=True)


@app.get("/")
def home():
    return FileResponse("/app/index.html", headers={"Cache-Control": "no-store"})


@app.get("/poster")
def poster():
    capture = cv2.VideoCapture(str(idle_video_path))
    ok, frame = capture.read()
    capture.release()
    if not ok:
        return Response(status_code=404)
    encoded, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    return Response(jpeg.tobytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/avatar/video")
def avatar_video():
    return FileResponse(idle_video_path, media_type="video/mp4",
                        headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    return {"ok": model is not None and render is not None, "model": "DH_live full",
            "publisher": publisher is not None, "viewers": len(viewers),
            "buffer_ms": buffer_ms, "frames_rendered": frames_rendered,
            "settle_ms": SETTLE_FRAMES * FRAME_MS,
            "settle_frames_rendered": settle_frames_rendered,
            "idle_video": idle_video_path.name,
            "avatar": avatar_config,
            "teeth": teeth.status() if teeth else None, "jpeg_quality": jpeg_quality,
            "mouth_clarity": clarity.status() if clarity else None,
            "last_render_ms": round(last_render_ms, 1), "error": last_error}


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


def seek_render(frame_index: int):
    capture = render._RenderModel__cap_input
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if frame_count <= 0:
        raise RuntimeError("avatar video contains no frames")
    render.frame_index = frame_index % frame_count
    capture.set(cv2.CAP_PROP_POS_FRAMES, render.frame_index)
    teeth.reset()


def infer(raw_frames: list[bytes], settling_pose=None) -> list[tuple[int, bytes]]:
    global frames_rendered, last_render_ms, last_mouth, settle_frames_rendered
    result = []
    with torch.inference_mode():
        for raw in raw_frames:
            start = time.perf_counter()
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            mouth = model.interface_frame(samples)
            if settling_pose is not None:
                mouth = settling_pose
                settle_frames_rendered += 1
            last_mouth = mouth.copy()
            # The prepared PKL already includes the reversed half of the video.
            # Wrap before upstream flips its landmark order for a second cycle.
            render.frame_index %= avatar_frame_count
            frame_index = render.frame_index
            frame = render.interface(mouth)
            frame = teeth.process(frame, frame_index)
            frame = clarity.process(frame, frame_index)
            encoded, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
            if not encoded:
                raise RuntimeError("JPEG encode failed")
            result.append((frame_index, jpeg.tobytes()))
            frames_rendered += 1
            last_render_ms = (time.perf_counter() - start) * 1000
    return result


@app.websocket("/publish")
async def publish(ws: WebSocket):
    global publisher, last_error, latest_frame, latest_frame_index
    global speech_active, requested_frame
    await ws.accept()
    if publisher is not None or model is None:
        await ws.close(code=1013, reason="model unavailable or publisher already connected")
        return
    publisher = ws
    pending = bytearray()
    converted = bytearray()
    resampler = StreamingResampler24To16()
    staged = deque()
    last_audio_at = 0.0
    needs_sync = False
    settling_from = None
    settling_step = 0
    settle_next_at = 0.
    speech_active = False
    await broadcast('{"type":"reset"}')
    try:
        while True:
            if staged:
                event = None
            else:
                try:
                    timeout = max(.001,settle_next_at-time.monotonic()) if settling_from is not None else .12
                    event = await asyncio.wait_for(ws.receive(), timeout=timeout)
                except asyncio.TimeoutError:
                    event = None
            if event and event.get("type") == "websocket.disconnect":
                break
            if event and event.get("bytes"):
                packet = event["bytes"]
                if len(packet) > 8 and (len(packet) - 8) % 2 == 0:
                    # New audio cancels the visual tail before another frame
                    # is generated, keeping interruptions responsive.
                    settling_from = None
                    settling_step = 0
                    if not speech_active:
                        speech_active = True
                        needs_sync = True
                        requested_frame = None
                        latest_frame = None
                        latest_frame_index = None
                        sync_event.clear()
                        await broadcast('{"type":"start"}')
                    last_audio_at = time.monotonic()
                    pending.extend(packet[8:])
            if event and event.get("text"):
                try:
                    control = json.loads(event["text"])
                except json.JSONDecodeError:
                    control = {}
                if control.get("type") == "reset":
                    was_active = speech_active
                    pending.clear()
                    converted.clear()
                    staged.clear()
                    settling_from = None
                    settling_step = 0
                    resampler.reset()
                    model.reset()
                    await asyncio.to_thread(teeth.reset)
                    if was_active and last_mouth is not None:
                        # An interruption stops audio immediately, but the
                        # visible face still settles unless new audio arrives.
                        last_audio_at = time.monotonic()-.25
                        needs_sync = False
                    else:
                        speech_active = False
                        await broadcast('{"type":"reset"}')
            if (speech_active and last_audio_at and time.monotonic() - last_audio_at > 0.24
                    and not staged and not pending):
                if converted:
                    # Do not discard a final incomplete 40 ms audio frame.
                    staged.append(bytes(converted).ljust(MODEL_FRAME_BYTES,b'\0'))
                    converted.clear()
                elif last_mouth is not None:
                    if settling_from is not None and time.monotonic() < settle_next_at:
                        continue
                    if settling_from is None:
                        settling_from = last_mouth.copy()
                        settling_step = 0
                        await broadcast('{"type":"settling"}')
                    started = time.monotonic()
                    settling_step += 1
                    pose = settle_mouth(settling_from,idle_mouth,settling_step/SETTLE_FRAMES)
                    images = await asyncio.to_thread(infer,[bytes(MODEL_FRAME_BYTES)],pose)
                    for frame_index,image in images:
                        latest_frame,latest_frame_index = image,frame_index
                        await broadcast(json.dumps({'type':'frame','index':frame_index,'phase':'settling'}))
                        await broadcast(image)
                    # Idle frames must remain at 25 fps, rather than arriving
                    # as a burst that the browser would drop from its queue.
                    settle_next_at = started + .04
                    if settling_step >= SETTLE_FRAMES:
                        speech_active = False
                        settling_from = None
                        await broadcast('{"type":"idle"}')
                    continue
                else:
                    speech_active = False
                    await broadcast('{"type":"idle"}')
            if len(pending) < BUFFER_BYTES and event is not None:
                continue
            if pending:
                converted.extend(resampler.push_pcm(bytes(pending)))
                pending.clear()
            while len(converted) >= MODEL_FRAME_BYTES:
                staged.append(bytes(converted[:MODEL_FRAME_BYTES]))
                del converted[:MODEL_FRAME_BYTES]
            if not staged:
                continue
            if needs_sync:
                if viewers:
                    try:
                        await asyncio.wait_for(sync_event.wait(), timeout=0.10)
                    except asyncio.TimeoutError:
                        pass
                if requested_frame is not None:
                    await asyncio.to_thread(seek_render, requested_frame)
                needs_sync = False
            # Process a bounded batch, allowing WebSocket control between batches.
            batch = [staged.popleft() for _ in range(min(len(staged), 5))]
            images = await asyncio.to_thread(infer, batch)
            for frame_index, image in images:
                latest_frame = image
                latest_frame_index = frame_index
                await broadcast(json.dumps({"type": "frame", "index": frame_index}))
                await broadcast(image)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"
        print(f"Full DH_live inference failed: {last_error}", flush=True)
    finally:
        if publisher is ws:
            publisher = None
            speech_active = False
            await broadcast('{"type":"reset"}')


@app.websocket("/view")
async def view(ws: WebSocket):
    global requested_frame
    await ws.accept()
    viewers.add(ws)
    if speech_active:
        await ws.send_text('{"type":"start"}')
    if speech_active and latest_frame is not None:
        await ws.send_text(json.dumps({"type": "frame", "index": latest_frame_index}))
        await ws.send_bytes(latest_frame)
    try:
        while True:
            event = await ws.receive()
            if event.get("type") == "websocket.disconnect":
                break
            if event.get("text"):
                try:
                    control = json.loads(event["text"])
                except json.JSONDecodeError:
                    continue
                if control.get("type") == "sync" and isinstance(control.get("frame"), int):
                    requested_frame = max(0, control["frame"])
                    sync_event.set()
    except WebSocketDisconnect:
        pass
    finally:
        viewers.discard(ws)
