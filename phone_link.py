"""USB-only Android phone signal and Samsung Routine notification bridge.

The HTTP listener binds to Windows loopback. ``adb reverse`` makes it reachable
as 127.0.0.1 on the phone. Every request still requires a random shared token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import ssl
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).parent
TOKEN_FILE = ROOT / "private" / "phone_link_token"
PORT = 18765
LAN_PORT = 18766
CERT_FILE = ROOT / "private" / "phone_link_cert.pem"
KEY_FILE = ROOT / "private" / "phone_link_key.pem"
COMMANDS = {"focus_on", "focus_off", "nudge", "guard"}
GUARD_MODES = {"off", "block", "block_lock"}
PACKAGE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
NUDGE_ACTIONS = {"accept", "snooze", "decline", "stay"}
COMMAND_TTL_SECONDS = 120
MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_STORED_AUDIO = 64
AUDIO_ID_PATTERN = re.compile(r"^[0-9a-f]{8,64}$")


def validate_nudge(payload: object) -> dict:
    """Check a nudge notification before it is queued for the phone.

    中文：提醒通知入队前校验：标题、正文、提醒编号和最多三个按钮。
    """
    if not isinstance(payload, dict):
        raise ValueError("nudge payload must be an object")
    title, text, nudge_id = payload.get("title"), payload.get("text"), payload.get("nudge_id")
    if not isinstance(title, str) or not 0 < len(title) <= 60:
        raise ValueError("nudge title must be 1-60 characters")
    if not isinstance(text, str) or not 0 < len(text) <= 400:
        raise ValueError("nudge text must be 1-400 characters")
    if not isinstance(nudge_id, str) or not 0 < len(nudge_id) <= 40:
        raise ValueError("nudge_id must be 1-40 characters")
    actions = payload.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 3:
        raise ValueError("a nudge has at most three actions")
    clean_actions = []
    for action in actions:
        if not isinstance(action, dict) or action.get("id") not in NUDGE_ACTIONS:
            raise ValueError("action id must be accept, snooze, or decline")
        label = action.get("label")
        if not isinstance(label, str) or not 0 < len(label) <= 40:
            raise ValueError("action label must be 1-40 characters")
        clean = {"id": action["id"], "label": label}
        if action["id"] == "snooze":
            minutes = action.get("snooze_minutes")
            if not isinstance(minutes, int) or isinstance(minutes, bool) or not 5 <= minutes <= 60:
                raise ValueError("snooze_minutes must be 5-60")
            clean["snooze_minutes"] = minutes
        clean_actions.append(clean)
    clean = {"title": title, "text": text, "nudge_id": nudge_id, "actions": clean_actions}
    # EN: Optional voice: a clip to auto-play and a longer one for "Stay with me".
    # 中文：可选语音：自动播放的短句，以及"Stay with me"使用的放松引导。
    if payload.get("guard") is not None:
        clean["guard"] = validate_guard(payload["guard"])
    for key in ("audio_id", "stay_audio_id"):
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not AUDIO_ID_PATTERN.match(value):
            raise ValueError(f"{key} must be a stored audio id")
        clean[key] = value
    return clean


def validate_guard(payload: object) -> dict:
    """A lockdown: off, or block / block_lock until a time with an allow list.

    中文：封锁指令：off，或 block / block_lock 到指定时间，并附放行名单与拦截语音。
    """
    if not isinstance(payload, dict) or payload.get("mode") not in GUARD_MODES:
        raise ValueError("guard mode must be off, block, or block_lock")
    if payload["mode"] == "off":
        return {"mode": "off"}
    until_ms = payload.get("until_ms")
    if not isinstance(until_ms, int) or isinstance(until_ms, bool) or until_ms <= 0:
        raise ValueError("guard until_ms must be a positive integer")
    allow = payload.get("allow")
    if (not isinstance(allow, list) or not 0 < len(allow) <= 200
            or not all(isinstance(p, str) and PACKAGE_PATTERN.match(p) for p in allow)):
        raise ValueError("guard allow must list 1-200 package names")
    clips = payload.get("bounce_audio_ids", [])
    if (not isinstance(clips, list) or len(clips) > 48
            or not all(isinstance(c, str) and AUDIO_ID_PATTERN.match(c) for c in clips)):
        raise ValueError("bounce_audio_ids must be stored audio ids")
    return {"mode": payload["mode"], "until_ms": until_ms, "allow": list(allow), "bounce_audio_ids": list(clips)}


def validate_guard_event(body: dict) -> dict:
    if body.get("event") != "bounce":
        raise ValueError("unknown guard event")
    package, label = body.get("package"), body.get("label", "")
    if not isinstance(package, str) or not PACKAGE_PATTERN.match(package):
        raise ValueError("package must be a package name")
    if not isinstance(label, str):
        raise ValueError("label must be text")
    return {"event": "bounce", "package": package, "label": label[:60]}


def validate_nudge_response(body: dict) -> dict:
    nudge_id, action = body.get("nudge_id"), body.get("action")
    if not isinstance(nudge_id, str) or not 0 < len(nudge_id) <= 40:
        raise ValueError("nudge_id must be 1-40 characters")
    if action not in NUDGE_ACTIONS:
        raise ValueError("action must be accept, snooze, or decline")
    response = {"nudge_id": nudge_id, "action": action}
    if action == "snooze":
        minutes = body.get("snooze_minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 5 <= minutes <= 60:
            raise ValueError("snooze_minutes must be 5-60")
        response["snooze_minutes"] = minutes
    return response


def token_from_file() -> str:
    TOKEN_FILE.parent.mkdir(exist_ok=True)
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="ascii").strip()
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token, encoding="ascii")
    return token


def classify(sample: dict, received_at: float, now: float | None = None) -> str:
    """Conservative, three-valued usage signal; never infer use from brightness."""
    now = time.time() if now is None else now
    if now - received_at > 45:
        return "unknown"
    if not sample.get("screen_interactive") or sample.get("device_locked"):
        return "idle"
    if not sample.get("usage_access"):
        return "unknown"
    age_ms = sample.get("last_interaction_age_ms")
    if isinstance(age_ms, (int, float)) and 0 <= age_ms <= 90_000:
        return "active"
    return "unknown"


@dataclass
class Command:
    id: str
    name: str
    created_at: float
    payload: dict = field(default_factory=dict)


class PhoneLink:
    def __init__(self, on_transition: Callable[[str, dict], None] | None = None,
                 on_sample: Callable[[str, dict], None] | None = None,
                 on_ack: Callable[[Command], None] | None = None,
                 on_expired: Callable[[Command], None] | None = None,
                 on_nudge_response: Callable[[dict], None] | None = None,
                 on_guard_event: Callable[[dict], None] | None = None):
        self.lock = threading.RLock()
        self.sample: dict = {}
        self.received_at = 0.0
        self.last_signal = "unknown"
        self.pending: deque[Command] = deque()
        self.on_transition = on_transition
        self.on_sample = on_sample
        # EN: Ack means the phone showed it; expiry means it never fetched it.
        # 中文：ack 表示手机已显示；过期表示手机始终没有领取。
        self.on_ack = on_ack
        self.on_expired = on_expired
        self.on_nudge_response = on_nudge_response
        self.on_guard_event = on_guard_event
        # EN: Small in-memory audio store the phone downloads from; ids are content hashes.
        # 中文：手机下载用的内存音频库，编号为内容哈希；只保留最近几段。
        self.audio: dict[str, bytes] = {}

    def ingest(self, sample: dict) -> str:
        if not isinstance(sample.get("screen_interactive"), bool):
            raise ValueError("screen_interactive must be boolean")
        if not isinstance(sample.get("device_locked"), bool):
            raise ValueError("device_locked must be boolean")
        if not isinstance(sample.get("usage_access"), bool):
            raise ValueError("usage_access must be boolean")
        with self.lock:
            first_sample = self.received_at == 0
            self.sample = sample
            self.received_at = time.time()
            signal = classify(sample, self.received_at)
            changed = first_sample or signal != self.last_signal
            self.last_signal = signal
        if changed and self.on_transition:
            self.on_transition(signal, sample)
        if self.on_sample:
            self.on_sample(signal, sample)
        return signal

    def status(self) -> dict:
        with self.lock:
            age = time.time() - self.received_at if self.received_at else None
            return {
                "signal": classify(self.sample, self.received_at) if self.received_at else "unknown",
                "sample_age_seconds": round(age, 1) if age is not None else None,
                "sample": dict(self.sample),
                "pending_commands": len(self.pending),
            }

    def expire_signal(self) -> None:
        with self.lock:
            if self.last_signal == "unknown" or not self.received_at or time.time() - self.received_at <= 45:
                return
            self.last_signal = "unknown"
            sample = dict(self.sample)
        if self.on_transition:
            self.on_transition("unknown", sample)

    def enqueue(self, name: str, payload: dict | None = None) -> Command:
        if name not in COMMANDS:
            raise ValueError("unsupported command")
        if name == "nudge":
            payload = validate_nudge(payload)
        elif name == "guard":
            payload = validate_guard(payload)
        elif payload:
            raise ValueError(f"{name} takes no payload")
        with self.lock:
            expired = self._expire_locked()
            if len(self.pending) >= 16:
                self._notify_expired(expired)
                raise ValueError("command queue full")
            command = Command(secrets.token_hex(12), name, time.time(), payload or {})
            self.pending.append(command)
        self._notify_expired(expired)
        return command

    def expire_commands(self) -> None:
        with self.lock:
            expired = self._expire_locked()
        self._notify_expired(expired)

    def _expire_locked(self) -> list[Command]:
        now = time.time()
        expired = [c for c in self.pending if now - c.created_at >= COMMAND_TTL_SECONDS]
        if expired:
            self.pending = deque(c for c in self.pending if now - c.created_at < COMMAND_TTL_SECONDS)
        return expired

    def _notify_expired(self, expired: list[Command]) -> None:
        for command in expired:
            if self.on_expired:
                try:
                    self.on_expired(command)
                except Exception:
                    LOG.exception("Phone command expiry callback failed")

    def next_command(self) -> dict | None:
        with self.lock:
            expired = self._expire_locked()
            command = self.pending[0] if self.pending else None
        self._notify_expired(expired)
        if command is None:
            return None
        value = {"id": command.id, "name": command.name}
        if command.payload:
            value["payload"] = dict(command.payload)
        return value

    def ack(self, command_id: str) -> bool:
        with self.lock:
            if not self.pending or self.pending[0].id != command_id:
                return False
            command = self.pending.popleft()
        if self.on_ack:
            try:
                self.on_ack(command)
            except Exception:
                LOG.exception("Phone command ack callback failed")
        return True

    def store_audio(self, data: bytes) -> str:
        if not 0 < len(data) <= MAX_AUDIO_BYTES:
            raise ValueError("audio must be 1 byte to 4 MB")
        audio_id = hashlib.sha256(data).hexdigest()[:32]
        with self.lock:
            self.audio.pop(audio_id, None)
            self.audio[audio_id] = data
            while len(self.audio) > MAX_STORED_AUDIO:
                self.audio.pop(next(iter(self.audio)))
        return audio_id

    def get_audio(self, audio_id: str) -> bytes | None:
        with self.lock:
            return self.audio.get(audio_id)

    def guard_event(self, body: dict) -> dict:
        event = validate_guard_event(body)
        if self.on_guard_event:
            self.on_guard_event(event)
        return event

    def nudge_response(self, body: dict) -> dict:
        response = validate_nudge_response(body)
        if self.on_nudge_response:
            self.on_nudge_response(response)
        return response


def make_server(link: PhoneLink, token: str, port: int = PORT,
                host: str = "127.0.0.1") -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            LOG.debug("phone link: " + format, *args)

        def reply(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}")

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("invalid body size")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("expected object")
            return value

        def do_GET(self):
            if not self.authorized():
                return self.reply(401, {"error": "unauthorized"})
            if self.path.startswith("/v1/audio/"):
                data = link.get_audio(self.path.removeprefix("/v1/audio/"))
                if data is None:
                    return self.reply(404, {"error": "audio not found"})
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return None
            if self.path == "/v1/status":
                return self.reply(200, link.status())
            if self.path == "/v1/commands/next":
                return self.reply(200, {"command": link.next_command()})
            return self.reply(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorized():
                return self.reply(401, {"error": "unauthorized"})
            if self.path == "/v1/audio":
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    if not 0 < length <= MAX_AUDIO_BYTES:
                        raise ValueError("audio must be 1 byte to 4 MB")
                    return self.reply(201, {"audio_id": link.store_audio(self.rfile.read(length))})
                except ValueError as exc:
                    return self.reply(400, {"error": str(exc)})
            try:
                body = self.read_json()
                if self.path == "/v1/status":
                    return self.reply(200, {"signal": link.ingest(body)})
                if self.path == "/v1/commands":
                    command = link.enqueue(body.get("name", ""), body.get("payload"))
                    return self.reply(202, {"id": command.id, "name": command.name})
                if self.path == "/v1/commands/ack":
                    return self.reply(200, {"acknowledged": link.ack(body.get("id", ""))})
                if self.path == "/v1/nudges/response":
                    return self.reply(200, {"recorded": link.nudge_response(body)})
                if self.path == "/v1/guard/event":
                    return self.reply(200, {"recorded": link.guard_event(body)})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self.reply(400, {"error": str(exc)})
            return self.reply(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), Handler)


def tls_fingerprint() -> str:
    """Create a persistent, self-signed TLS identity for the local LAN bridge."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timedelta, timezone

    CERT_FILE.parent.mkdir(exist_ok=True)
    if not CERT_FILE.exists() or not KEY_FILE.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Specter Phone Link")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        KEY_FILE.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()))
        CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex()


def start_background(on_transition=None, on_sample=None, port: int = PORT, *,
                     on_ack=None, on_expired=None, on_nudge_response=None,
                     on_guard_event=None) -> tuple[PhoneLink, ThreadingHTTPServer]:
    link = PhoneLink(on_transition, on_sample, on_ack, on_expired, on_nudge_response, on_guard_event)
    server = make_server(link, token_from_file(), port)
    threading.Thread(target=server.serve_forever, name="phone-link", daemon=True).start()
    if os.getenv("SPECTER_PHONE_LAN_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
        fingerprint = tls_fingerprint()
        lan_host = os.getenv("SPECTER_PHONE_LAN_BIND", "0.0.0.0")
        lan_port = int(os.getenv("SPECTER_PHONE_LAN_PORT", str(LAN_PORT)))
        try:
            lan_server = make_server(link, token_from_file(), lan_port, lan_host)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
            lan_server.socket = context.wrap_socket(lan_server.socket, server_side=True)
            threading.Thread(target=lan_server.serve_forever, name="phone-link-lan", daemon=True).start()
            LOG.info("Phone LAN link listening on %s:%d (TLS SHA-256 %s)", lan_host, lan_port, fingerprint)
        except OSError:
            LOG.exception("Phone LAN link could not listen on %s:%d", lan_host, lan_port)
    def watch_staleness():
        while True:
            time.sleep(5)
            link.expire_signal()
            link.expire_commands()
    threading.Thread(target=watch_staleness, name="phone-link-staleness", daemon=True).start()
    LOG.info("Phone link listening on 127.0.0.1:%d", port)
    return link, server


def request(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=data,
        headers={"Authorization": f"Bearer {token_from_file()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def upload_audio(data: bytes) -> str:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/audio", data=data,
        headers={"Authorization": f"Bearer {token_from_file()}", "Content-Type": "audio/mpeg"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)["audio_id"]


def release_tonight() -> None:
    """Computer backdoor: lift the phone lockdown and stop bedtime nudges until morning.

    中文：电脑端后门：解除手机封锁，并让今晚到早上不再催睡。需要走到电脑前运行。
    """
    from datetime import datetime

    from phone_guard import next_morning
    from situation import SituationSettings

    until = next_morning(datetime.now(), SituationSettings.from_env().morning)
    print(json.dumps(request("/v1/commands", {"name": "guard", "payload": {"mode": "off"}}), ensure_ascii=False))
    try:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.connect(os.getenv("SPECTER_MQTT_HOST", "127.0.0.1"), int(os.getenv("SPECTER_MQTT_PORT", "1884")))
        client.loop_start()
        stamp = int(time.time() * 1000)
        client.publish("specter/situation/quiet", json.dumps({
            "until_ms": int(until.timestamp() * 1000), "reason": "backdoor", "emitted_at_ms": stamp,
        }), retain=True, qos=1).wait_for_publish(5)
        client.publish("specter/phone/guard", json.dumps({
            "event": "released", "emitted_at_ms": stamp,
        }), qos=1).wait_for_publish(5)
        client.loop_stop()
        client.disconnect()
    except Exception as exc:
        print(f"Phone unlocked, but bedtime nudges could not be paused ({type(exc).__name__}).")
        return
    print(f"Phone lockdown lifted; no bedtime nudges until {until:%H:%M}. The phone applies it within 10 seconds.")


def main():
    parser = argparse.ArgumentParser(description="Local USB Android phone link")
    parser.add_argument("action", choices=["serve", "status", "send", "test-nudge", "release", "test-lock"])
    parser.add_argument("--mode", choices=["block", "block_lock"], default="block",
                        help="test-lock only: block, or block_lock (auto-lock after unlock and dim)")
    parser.add_argument("name", nargs="?", choices=sorted(COMMANDS))
    args = parser.parse_args()
    if args.action == "serve":
        logging.basicConfig(level=logging.INFO)
        _, server = start_background(lambda signal, sample: LOG.info("Phone signal: %s (%s)", signal, sample.get("foreground_package")))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.shutdown()
    elif args.action == "status":
        print(json.dumps(request("/v1/status"), ensure_ascii=False, indent=2))
    elif args.action == "release":
        release_tonight()
    elif args.action == "test-lock":
        # EN: A 3-minute lockdown that expires on its own, to try the bounce safely.
        # 中文：3 分钟后自动失效的测试封锁，用来安全地试一下"弹回桌面"。
        from phone_guard import allow_list, bounce_audio
        from nudge_voice import load_openai_env

        load_openai_env()
        clip_ids = [upload_audio(clip) for clip in bounce_audio()]
        payload = {
            "mode": args.mode, "until_ms": int((time.time() + 180) * 1000),
            "allow": allow_list(), "bounce_audio_ids": clip_ids,
        }
        print(json.dumps(request("/v1/commands", {"name": "guard", "payload": payload}), ensure_ascii=False))
        print("Test lockdown for 3 minutes; run `phone_link.py release` to end it sooner.")
    elif args.action == "test-nudge":
        # EN: Send one sample reminder, spoken in his voice, with the real buttons.
        # 中文：发送一条示例提醒，用他的声音朗读，带真实按钮，检查显示、播放和回传。
        from nudge_prompt import phone_nudge_payload
        from nudge_voice import load_openai_env, stay_audio, synthesize

        load_openai_env()
        text = "Test from Specter. If you can hear me, try Stay with me."
        audio_id = upload_audio(synthesize(text))
        stay_id = upload_audio(stay_audio())
        payload = phone_nudge_payload(
            {"id": "test-" + secrets.token_hex(4), "level": 1}, text, audio_id, stay_id,
        )
        print(json.dumps(request("/v1/commands", {"name": "nudge", "payload": payload}), ensure_ascii=False))
    else:
        if not args.name:
            parser.error("send requires focus_on or focus_off")
        print(json.dumps(request("/v1/commands", {"name": args.name}), ensure_ascii=False))


if __name__ == "__main__":
    main()
