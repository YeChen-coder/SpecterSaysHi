"""Small file-based control channel for starting and closing voice sessions."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path


_ENGLISH_CLOSE = re.compile(
    r"(?:please\s+)?(?:can you\s+|could you\s+|would you\s+|i want (?:you )?to\s+)?"
    r"(?:end|close|stop)\s+(?:(?:this|the|our|current)\s+)?(?:conversation|session)"
    r"(?:\s+(?:now|please))?[.!?]?",
    re.IGNORECASE,
)
_CHINESE_CLOSE = re.compile(r"(?:请)?(?:结束|关闭)(?:这次|当前|这个)?(?:对话|会话)(?:吧|谢谢)?[。.!?]?")


def explicit_close_phrase(text: str) -> bool:
    """Match a short, direct spoken request to end the current session."""
    text = text.strip()
    return bool(
        len(text) <= 120
        and (
            _ENGLISH_CLOSE.fullmatch(text)
            or _CHINESE_CLOSE.fullmatch(text)
            or text.casefold() in {"stop listening", "stop listening.", "别再听了", "别再听了。"}
        )
    )


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_active(directory: Path, session_id: str) -> None:
    _write_json(directory / "active_session.json", {"session_id": session_id, "pid": os.getpid()})


def request_start(directory: Path) -> str:
    request_id = uuid.uuid4().hex
    _write_json(directory / "start_session.json", {"request_id": request_id})
    return request_id


def consume_start_request(directory: Path) -> str | None:
    path = directory / "start_session.json"
    claimed = directory / f"start_session.{uuid.uuid4().hex}.claimed"
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return None
    try:
        request = _read_json(claimed)
        request_id = request.get("request_id") if request else None
        return request_id if isinstance(request_id, str) and request_id else None
    finally:
        claimed.unlink(missing_ok=True)


def _active_session_id(directory: Path) -> str | None:
    active = _read_json(directory / "active_session.json")
    session_id = active.get("session_id") if active else None
    return session_id if isinstance(session_id, str) and session_id else None


def request_close(directory: Path) -> str | None:
    session_id = _active_session_id(directory)
    if session_id is None:
        return None
    publish_close_status(directory, session_id, "transcribing")
    _write_json(directory / "close_session.json", {"session_id": session_id})
    return session_id


def publish_close_status(
    directory: Path, session_id: str, stage: str, result: str | None = None,
) -> None:
    _write_json(directory / "close_session_status.json", {
        "session_id": session_id, "stage": stage, "result": result,
    })


def read_close_status(directory: Path, session_id: str) -> dict | None:
    status = _read_json(directory / "close_session_status.json")
    return status if status and status.get("session_id") == session_id else None


def close_requested(directory: Path, session_id: str) -> bool:
    request = _read_json(directory / "close_session.json")
    return bool(request and request.get("session_id") == session_id)


def clear_active(directory: Path, session_id: str) -> None:
    for name in ("active_session.json", "close_session.json"):
        path = directory / name
        value = _read_json(path)
        if value and value.get("session_id") == session_id:
            path.unlink(missing_ok=True)


def wait_for_close(directory: Path, session_id: str, *, timeout: float = 120) -> int:
    print("Close requested. Finishing transcription...", flush=True)
    deadline = time.monotonic() + timeout
    saving_announced = False
    while time.monotonic() < deadline:
        status = read_close_status(directory, session_id)
        if status and status.get("stage") == "saving" and not saving_announced:
            print("Realtime connection closed. Saving session memory...", flush=True)
            saving_announced = True
        if status and status.get("stage") == "done":
            result = status.get("result")
            if result == "saved":
                print("Voice session closed. Session memory saved.")
                return 0
            if result == "saved_partial":
                print("Voice session closed. Memory saved from an incomplete transcription.")
                return 0
            if result == "no_memory":
                print("Voice session closed. Nothing worth saving as memory.")
                return 0
            if result == "no_transcript":
                print("Voice session closed. No user transcription was available.")
                return 0
            print("Voice session closed, but memory saving failed. Check logs/specter.log.")
            return 1
        time.sleep(0.1)
    print("Close is still finishing. Check logs/specter.log for progress.")
    return 1


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1].lower() not in {"start", "close"}:
        print("Usage: python session_control.py start|close", file=sys.stderr)
        return 2
    directory = Path(__file__).with_name("logs")
    if sys.argv[1].lower() == "start":
        if _active_session_id(directory):
            print("A voice session is already active.")
            return 0
        request_id = request_start(directory)
        for _ in range(150):
            if _active_session_id(directory):
                print("Voice session started.")
                return 0
            time.sleep(0.1)
        pending = _read_json(directory / "start_session.json")
        if pending and pending.get("request_id") == request_id:
            (directory / "start_session.json").unlink(missing_ok=True)
            print("Voice listener did not receive the start request. Check that it is running.")
        else:
            print("Start request received, but no session opened. Check logs/specter.log.")
        return 1
    session_id = request_close(directory)
    if session_id is None:
        print("No active voice session.")
        return 1
    return wait_for_close(directory, session_id)


if __name__ == "__main__":
    raise SystemExit(main())
