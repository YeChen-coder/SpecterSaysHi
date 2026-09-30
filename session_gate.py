"""Reserve the one microphone and speaker for a single voice session."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

LOG = logging.getLogger("specter")


class SessionGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_kind: str | None = None

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

            try:
                threading.Thread(target=worker, name=f"realtime-{kind}", daemon=True).start()
            except Exception:
                self._active_kind = None
                raise
        return True
