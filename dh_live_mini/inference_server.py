"""Stateful CPU inference service replacing DHLiveMini.wasm audio and image calls."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from independent_image_renderer import IndependentImageRenderer
from streaming_blendshape import RealtimeBlendShape

SIZE = 184
IMAGE_BYTES = SIZE * SIZE * 4
ORIGIN = os.getenv("BROWSER_ORIGIN", "http://127.0.0.1:18888")
AUDIO_CHECKPOINT = os.getenv("AUDIO_CHECKPOINT", "/checkpoint/lstm/lstm_model_epoch_325.pkl")
BUFFER_MS = int(os.getenv("DH_LIVE_AUDIO_BUFFER_MS", "100"))
image = IndependentImageRenderer(
    os.getenv("IMAGE_CHECKPOINT", "/checkpoint/DINet_mini/epoch_40_new.pth"),
    os.getenv("AVATAR_DATA", "/avatar/combined_data.json.gz"),
)
image_lock = threading.Lock()
sessions_lock = threading.Lock()
sessions = {}


class AudioSession:
    def __init__(self):
        self.audio = RealtimeBlendShape(AUDIO_CHECKPOINT)
        self.lock = threading.Lock()
        self.last_seen = time.monotonic()
        self.energy_pending = bytearray()
        self.energy = []
        self.raw = {}
        self.last_emitted = -1

    def reset(self):
        with self.lock:
            self.audio.reset()
            self.energy_pending.clear()
            self.energy.clear()
            self.raw.clear()
            self.last_emitted = -1

    def push(self, pcm):
        if len(pcm) % 2:
            raise ValueError("24 kHz PCM must have complete int16 samples")
        with self.lock:
            self.energy_pending.extend(pcm)
            while len(self.energy_pending) >= 1920:
                block = bytes(self.energy_pending[:1920])
                del self.energy_pending[:1920]
                samples = np.frombuffer(block, dtype="<i2").astype(np.float32) / 32768.0
                self.energy.append(float(np.sqrt(np.mean(samples * samples))))
            for index, blend in self.audio.push_pcm_24k(pcm):
                self.raw[index] = blend
            frames = []
            while self.last_emitted + 2 in self.raw:
                index = self.last_emitted + 1
                center = self.raw[index]
                before = self.raw.get(index - 1, center)
                after = self.raw[index + 1]
                rms = self.energy[index] if index < len(self.energy) else 0.0
                gain = min(1.0, max(0.0, (rms - 0.008) / 0.045))
                scaled = (0.25 * before + 0.5 * center + 0.25 * after) * (0.32 * gain)
                values = 28.0 * np.tanh(scaled / 28.0)
                frames.append({"frame": index, "blend": values.astype(float).tolist()})
                self.last_emitted = index
                self.raw.pop(index - 2, None)
            return frames


def get_session(key):
    now = time.monotonic()
    with sessions_lock:
        for expired in [name for name, state in sessions.items()
                        if now - state.last_seen > 600]:
            del sessions[expired]
        if key not in sessions:
            if len(sessions) >= 8:
                del sessions[min(sessions, key=lambda name: sessions[name].last_seen)]
            sessions[key] = AudioSession()
        sessions[key].last_seen = now
        return sessions[key]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

    def headers_for(self, kind, length):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def send_json(self, value):
        body = json.dumps(value).encode("utf-8")
        self.headers_for("application/json", len(body))
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.headers_for("text/plain", 0)

    def do_GET(self):
        if self.path == "/health":
            self.send_json({"ready": True, "model_size": SIZE, "sample_rate": 24000,
                            "frame_ms": 40, "packet_ms": BUFFER_MS})
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            session_key = self.headers.get("X-Specter-Session", "default")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_key):
                raise ValueError("invalid audio session")
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > IMAGE_BYTES * 2:
                raise ValueError("invalid body size")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("truncated body")
            if self.path == "/audio/reset":
                get_session(session_key).reset()
                self.send_json({"ok": True})
            elif self.path == "/audio/push":
                self.send_json({"frames": get_session(session_key).push(body)})
            elif self.path == "/render":
                if len(body) != 2 * IMAGE_BYTES:
                    raise ValueError("render requires source RGBA and geometry RGBA")
                with image_lock:
                    result = image.render(body[:IMAGE_BYTES], body[IMAGE_BYTES:])
                self.headers_for("application/octet-stream", len(result))
                self.wfile.write(result)
            else:
                self.send_error(404)
        except (ValueError, IndexError) as exc:
            self.send_error(400, str(exc))
        except Exception:
            self.log_error("inference failed")
            raise


if __name__ == "__main__":
    torch_threads = int(os.getenv("TORCH_NUM_THREADS", "2"))
    import torch
    torch.set_num_threads(torch_threads)
    server = ThreadingHTTPServer(("0.0.0.0", 8899), Handler)
    print("Independent inference ready on port 8899", flush=True)
    server.serve_forever()
