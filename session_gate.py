"""Reserve the one microphone and speaker for a single voice session."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

LOG = logging.getLogger("specter")


class SessionGate:
    def __init__(self, on_change: Callable[[str | None], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._active_kind: str | None = None
        # EN: Reports the active kind, or None once released (outside the lock).
        # 中文：报告当前占用的会话类型，释放后报告 None；回调在锁外执行。
        self._on_change = on_change

    def _notify(self, kind: str | None) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(kind)
        except Exception:
            LOG.exception("Session state callback failed")

    @property
    def active_kind(self) -> str | None:
        with self._lock:
            return self._active_kind

    def start(self, kind: str, run: Callable[[], None]) -> bool:
        """Reserve before starting the worker; release after all cleanup finishes."""
        with self._lock:
            if self._active_kind is not None:
                return False
            self._active_kind = kind

        def worker() -> None:
            try:
                run()
            except Exception:
                LOG.exception("%s voice session failed", kind)
            finally:
                with self._lock:
                    self._active_kind = None
                self._notify(None)

        # EN: Announce before the worker starts so "released" can never precede it.
        # 中文：先通知占用再启动工作线程，保证"释放"通知不会早于"占用"。
        self._notify(kind)
        try:
            threading.Thread(target=worker, name=f"realtime-{kind}", daemon=True).start()
        except Exception:
            with self._lock:
                self._active_kind = None
            self._notify(None)
            raise
        return True
