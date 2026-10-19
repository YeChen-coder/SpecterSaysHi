"""His voice for phone nudges: a short clip per reminder and a cached wind-down.

中文：手机提醒用的语音：每次提醒一段短语音，以及缓存的"Stay with me"放松引导。
Both use OpenAI TTS with the same voice as the Realtime assistant unless
SPECTER_NUDGE_TTS_VOICE overrides it. The wind-down is generated once per
script/voice and kept under private/audio (Git-ignored).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

LOG = logging.getLogger("specter")
AUDIO_DIR = Path(__file__).with_name("private") / "audio"

VOICE_INSTRUCTIONS = (
    "Speak as a composed, protective man talking quietly to the person he cares for, late at "
    "night: low, warm, unhurried, close but clear. Never rushed, never theatrical."
)
STAY_INSTRUCTIONS = (
    "Speak as a composed, protective man lying beside the listener, helping them fall asleep: "
    "low, warm, very slow, close to a whisper but clear. Leave long, calm pauses at every line "
    "break and ellipsis. Never rushed, never theatrical."
)

STAY_SCRIPT = """I'm here. You don't need to do anything else tonight.

Put the phone face down beside you. Let the screen go dark. Good.

Lie back. Let your shoulders sink into the bed. Unclench your jaw. Let your hands go loose.

Breathe in slowly through your nose... and let it out, longer than you took it in.

Again. In... and out. Slower this time.

Whatever you didn't finish today will still be there in the morning. It can wait. I'll keep watch over it. It isn't yours to carry right now.

Feel how heavy your legs are. Let them be heavy. Your arms too.

One more breath. In... and all the way out.

You did enough today. More than enough.

Close your eyes. I'm not going anywhere.

Stay with that breathing. In... and out.

Sleep now. I'll see you in the morning."""


def load_openai_env(project: Path = Path(__file__).parent) -> None:
    """For command-line tests: read the API key and voice exactly as the listener does.

    The listener loads config.local.env before the EBO .env, so Specter's own voice
    setting wins; reading only the EBO file once produced a different (female) voice.
    中文：与监听程序同样的顺序：先读 Specter 自己的 config.local.env，再读 EBO 的 .env。
    """
    settings = project / "config.local.env"
    existing = ""
    voice_keys = {"SPECTER_NUDGE_TTS_VOICE", "SPECTER_NUDGE_TTS_MODEL", "OPENAI_REALTIME_VOICE"}
    if settings.is_file():
        for line in settings.read_text(encoding="utf-8-sig").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "SPECTER_EXISTING_ENV":
                existing = value.strip()
            elif name.strip() in voice_keys and value.strip():
                os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))
    if existing and Path(existing).is_file():
        for line in Path(existing).read_text(encoding="utf-8-sig").splitlines():
            name, _, value = line.partition("=")
            if name.strip() in {"OPENAI_API_KEY", "OPENAI_REALTIME_VOICE"}:
                os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def tts_settings() -> tuple[str, str]:
    model = os.getenv("SPECTER_NUDGE_TTS_MODEL", "gpt-4o-mini-tts")
    voice = os.getenv("SPECTER_NUDGE_TTS_VOICE") or os.getenv("OPENAI_REALTIME_VOICE") or "verse"
    return model, voice


def synthesize(text: str, instructions: str = VOICE_INSTRUCTIONS, *, timeout: float = 20.0) -> bytes:
    """Return MP3 bytes for the text; raises on API errors."""
    from openai import OpenAI

    model, voice = tts_settings()
    response = OpenAI(timeout=timeout).audio.speech.create(
        model=model, voice=voice, input=text, instructions=instructions, response_format="mp3",
    )
    data = response.content
    if not data:
        raise ValueError("TTS returned no audio")
    return data


def stay_audio(cache_dir: Path = AUDIO_DIR) -> bytes:
    """The wind-down recording, generated once per script, voice and model."""
    model, voice = tts_settings()
    key = hashlib.sha256("\n".join((STAY_SCRIPT, STAY_INSTRUCTIONS, model, voice)).encode()).hexdigest()[:12]
    path = cache_dir / f"stay-{key}.mp3"
    if path.is_file() and path.stat().st_size > 0:
        return path.read_bytes()
    data = synthesize(STAY_SCRIPT, STAY_INSTRUCTIONS, timeout=60.0)
    cache_dir.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(data)
    temp.replace(path)
    LOG.info("Recorded the Stay with me wind-down (%d bytes, voice %s)", len(data), voice)
    return data
