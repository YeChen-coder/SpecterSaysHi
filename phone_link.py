"""USB-only Android phone signal and Samsung Routine notification bridge.

The HTTP listener binds to Windows loopback. ``adb reverse`` makes it reachable
as 127.0.0.1 on the phone. Every request still requires a random shared token.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import ssl
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
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
COMMANDS = {"focus_on", "focus_off"}


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


class PhoneLink:
    def __init__(self, on_transition: Callable[[str, dict], None] | None = None,
                 on_sample: Callable[[str, dict], None] | None = None):
        self.lock = threading.RLock()
        self.sample: dict = {}
        self.received_at = 0.0
        self.last_signal = "unknown"
        self.pending: deque[Command] = deque()
        self.on_transition = on_transition
        self.on_sample = on_sample

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

    def enqueue(self, name: str) -> Command:
        if name not in COMMANDS:
            raise ValueError("unsupported command")
        with self.lock:
            self._expire()
            if len(self.pending) >= 16:
                raise ValueError("command queue full")
            command = Command(secrets.token_hex(12), name, time.time())
            self.pending.append(command)
            return command

    def _expire(self) -> None:
        now = time.time()
        self.pending = deque(c for c in self.pending if now - c.created_at < 120)

    def next_command(self) -> dict | None:
        with self.lock:
            self._expire()
            if not self.pending:
                return None
            command = self.pending[0]
            return {"id": command.id, "name": command.name}

    def ack(self, command_id: str) -> bool:
        with self.lock:
            if self.pending and self.pending[0].id == command_id:
                self.pending.popleft()
                return True
            return False


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
            if self.path == "/v1/status":
                return self.reply(200, link.status())
            if self.path == "/v1/commands/next":
                return self.reply(200, {"command": link.next_command()})
            return self.reply(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorized():
                return self.reply(401, {"error": "unauthorized"})
            try:
                body = self.read_json()
                if self.path == "/v1/status":
                    return self.reply(200, {"signal": link.ingest(body)})
                if self.path == "/v1/commands":
                    command = link.enqueue(body.get("name", ""))
                    return self.reply(202, {"id": command.id, "name": command.name})
                if self.path == "/v1/commands/ack":
                    return self.reply(200, {"acknowledged": link.ack(body.get("id", ""))})
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


def start_background(on_transition=None, on_sample=None, port: int = PORT) -> tuple[PhoneLink, ThreadingHTTPServer]:
    link = PhoneLink(on_transition, on_sample)
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


def main():
    parser = argparse.ArgumentParser(description="Local USB Android phone link")
    parser.add_argument("action", choices=["serve", "status", "send"])
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
    else:
        if not args.name:
            parser.error("send requires focus_on or focus_off")
        print(json.dumps(request("/v1/commands", {"name": args.name}), ensure_ascii=False))


if __name__ == "__main__":
    main()
