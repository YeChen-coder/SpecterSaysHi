"""Is the owner available to talk? Camera cover and other apps' dictation.

中文：用户此刻能不能聊：摄像头盖子开没开，以及有没有别的程序正在听写。
- Cover: Specter's camera has a physical shutter. A near-black frame held for a
  while means "do not disturb"; opening it means available again.
  盖子：画面持续接近全黑表示勿扰；打开即恢复可打扰。
- Dictation: Windows records, per app, when it last started and stopped using
  the microphone. While a dictation app (Typeless, Codex, ChatGPT, Claude, a
  browser, WeChat) is using it, the owner is talking to something else.
  听写：Windows 按应用记录麦克风开始/停止使用时间；听写类应用正在用麦克风时，用户在跟别的东西说话。
"""

from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass

MIC_STORE = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
DEFAULT_DICTATION_APPS = "typeless,codex,chatgpt,claude,msedge,chrome,firefox,weixin"


def frame_brightness(jpeg: bytes) -> float:
    """Average luminance (0-255) of a JPEG frame."""
    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(jpeg)) as image:
        gray = image.convert("L")
        gray.thumbnail((160, 160))
        return float(ImageStat.Stat(gray).mean[0])


@dataclass
class CoverDetector:
    """Hysteresis on frame brightness: closed after 10 s dark, open after 3 s bright.

    中文：亮度滞回判断：持续 10 秒很暗才算盖上，持续 3 秒变亮才算打开，避免手挡一下就误判。
    """

    dark_below: float = 25.0
    close_after_seconds: float = 10.0
    open_after_seconds: float = 3.0
    covered: bool = False
    _since: float | None = None

    def observe(self, brightness: float, now: float) -> bool | None:
        """Return the new state when it changes, otherwise None."""
        dark = brightness < self.dark_below
        if dark == self.covered:
            self._since = None
            return None
        if self._since is None:
            self._since = now
        wait = self.close_after_seconds if dark else self.open_after_seconds
        if now - self._since < wait:
            return None
        self.covered, self._since = dark, None
        return self.covered


def _app_name(key_name: str) -> str:
    """NonPackaged keys are paths with '#' for '\\'; packaged keys are family names."""
    return key_name.replace("#", "\\").rsplit("\\", 1)[-1]


def apps_using_microphone() -> list[str]:
    """Apps Windows currently records as using the microphone (empty off Windows)."""
    if sys.platform != "win32":
        return []
    import winreg

    in_use: list[str] = []

    def scan(path: str) -> None:
        try:
            parent = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return
        with parent:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(parent, index)
                except OSError:
                    break
                index += 1
                if name == "NonPackaged":
                    continue
                try:
                    with winreg.OpenKey(parent, name) as key:
                        start = winreg.QueryValueEx(key, "LastUsedTimeStart")[0]
                        stop = winreg.QueryValueEx(key, "LastUsedTimeStop")[0]
                except OSError:
                    continue
                if start and stop == 0:
                    in_use.append(name)

    scan(MIC_STORE)
    scan(MIC_STORE + r"\NonPackaged")
    return in_use


def dictating_app(in_use: list[str] | None = None, own_executable: str = sys.executable) -> str | None:
    """The first watched app now using the microphone, excluding Specter itself.

    中文：返回正在用麦克风的听写类应用名（排除 Specter 自己的 Python 进程）。
    """
    patterns = [
        p.strip().lower()
        for p in os.getenv("SPECTER_DICTATION_APPS", DEFAULT_DICTATION_APPS).split(",") if p.strip()
    ]
    own = os.path.normcase(own_executable).replace("\\", "#")
    for key_name in apps_using_microphone() if in_use is None else in_use:
        if os.path.normcase(key_name) == own:
            continue
        name = _app_name(key_name)
        if any(pattern in name.lower() for pattern in patterns):
            return name
    return None
