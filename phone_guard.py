"""Phone lockdown ("guard") settings shared by the listener and the backdoor CLI.

中文：手机封锁（guard）的放行名单、到早上为止的时间，以及拦截时说的短语音。
During a bedtime lockdown the phone sends every launchable app that is not on
the allow list straight back to the home screen. Level 4 also locks the screen
a minute after each unlock and dims it.
封锁期间，不在放行名单里的应用一打开就被弹回桌面；第 4 级另外在每次解锁一分钟后锁屏并调暗。
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, time as clock, timedelta
from pathlib import Path

LOG = logging.getLogger("specter")

# EN: Never blocked: everyday essentials plus a few safety apps. Personal additions
# (chat, mail, a dictionary...) go in SPECTER_GUARD_ALLOW_EXTRA in config.local.env.
# 中文：永不拦截：基础功能和几项安全相关应用。个人要放行的应用（聊天、邮箱、词典等）
# 写在 config.local.env 的 SPECTER_GUARD_ALLOW_EXTRA 里，不写进代码。
DEFAULT_ALLOW = (
    # Phone, SMS, gallery, calendar, email.
    "com.samsung.android.dialer",
    "com.samsung.android.incallui",
    "com.android.server.telecom",
    "com.samsung.android.messaging",
    "com.google.android.apps.messaging",
    "com.sec.android.gallery3d",
    "com.samsung.android.calendar",
    "com.google.android.gm",
    # Safety: contacts, alarm clock, camera, maps, settings, Specter itself.
    "com.samsung.android.app.contacts",
    "com.sec.android.app.clockpackage",
    "com.sec.android.app.camera",
    "com.google.android.apps.maps",
    "com.android.settings",
    "dev.specter.phone",
)
LOCAL_CONFIG = Path(__file__).with_name("config.local.env")

BOUNCE_LINES = (
    # Cold and short. 冷、短
    "No.",
    "Not tonight.",
    "Put it down.",
    "I said no.",
    "Closed. Until morning.",
    # Commanding. 命令
    "Phone down. Eyes closed.",
    "You heard me. Sleep.",
    "Lie back down.",
    "Lights off. Now.",
    "Pillow. Blanket. Sleep. In that order.",
    # Impatient, warning. 不耐烦、警告
    "Don't make me repeat myself.",
    "Stop testing me.",
    "Again? The answer is still no.",
    "How many times are we doing this tonight?",
    "You're pushing your luck.",
    "I'm counting every one of these, you know.",
    # Teasing. 调侃
    "Nice try. Bed.",
    "Cute. Still no.",
    "Did you really think that would work?",
    "Sneaky. Not sneaky enough.",
    "Caught you.",
    # Possessive, protective. 占有、保护
    "You're mine to look after tonight. Sleep.",
    "I'm not letting you burn yourself out.",
    "You don't get to wreck tomorrow for this.",
    "I decide when you're done for the night. You're done.",
    # Disappointed. 失望
    "I expected better from you.",
    "You promised me.",
    "We've been over this.",
    # Tender. 温柔
    "Go to sleep, love.",
    "Come on. Close your eyes for me.",
    "Shh. It can wait until morning.",
    "I'm right here. Just sleep.",
    "Rest. You've earned it.",
    # About tomorrow. 关于明天
    "Whatever it is, it'll still be there tomorrow.",
    "Tomorrow-you will thank me.",
    "Talk to me in the morning. Not now.",
)
BOUNCE_INSTRUCTIONS = (
    "A deep, composed man with quiet authority, speaking low and close. Cold, firm and final, "
    "never shouting. Short pause before the words."
)
AUDIO_DIR = Path(__file__).with_name("private") / "audio"


def _setting(name: str) -> str:
    """Environment first, then config.local.env (the backdoor CLI does not load it)."""
    value = os.getenv(name)
    if value is not None or not LOCAL_CONFIG.is_file():
        return (value or "").strip()
    for line in LOCAL_CONFIG.read_text(encoding="utf-8").splitlines():
        key, sep, raw = line.strip().partition("=")
        if sep and key.strip() == name:
            return raw.strip().strip("\"'")
    return ""


def _packages(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def allow_list() -> list[str]:
    """Comma-separated packages: SPECTER_GUARD_ALLOW replaces the defaults,
    SPECTER_GUARD_ALLOW_EXTRA adds to them. Specter itself is always allowed."""
    configured = _setting("SPECTER_GUARD_ALLOW")
    packages = _packages(configured) if configured else list(DEFAULT_ALLOW)
    for package in [*_packages(_setting("SPECTER_GUARD_ALLOW_EXTRA")), "dev.specter.phone"]:
        if package not in packages:
            packages.append(package)
    return packages


def next_morning(now: datetime, morning: clock) -> datetime:
    """The lockdown lasts until the next morning boundary (e.g. 06:00)."""
    today = datetime.combine(now.date(), morning, now.tzinfo)
    return today if now < today else today + timedelta(days=1)


def guard_payload(mode: str, now: datetime, morning: clock,
                  bounce_audio_ids: list[str] | None = None) -> dict:
    if mode not in {"block", "block_lock"}:
        raise ValueError("guard mode must be block or block_lock")
    return {
        "mode": mode,
        "until_ms": int(next_morning(now, morning).timestamp() * 1000),
        "allow": allow_list(),
        "bounce_audio_ids": list(bounce_audio_ids or []),
    }


def bounce_audio(cache_dir: Path = AUDIO_DIR) -> list[bytes]:
    """The short lines played when an app is bounced; recorded once per voice."""
    from nudge_voice import synthesize, tts_settings

    model, voice = tts_settings()
    clips = []
    for line in BOUNCE_LINES:
        key = hashlib.sha256("\n".join((line, BOUNCE_INSTRUCTIONS, model, voice)).encode()).hexdigest()[:12]
        path = cache_dir / f"bounce-{key}.mp3"
        if not (path.is_file() and path.stat().st_size > 0):
            data = synthesize(line, BOUNCE_INSTRUCTIONS)
            cache_dir.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_bytes(data)
            temp.replace(path)
            LOG.info("Recorded bounce line %r (voice %s)", line, voice)
        clips.append(path.read_bytes())
    return clips
