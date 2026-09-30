"""Keep Specter log slices small and discard old rotated slices."""

from __future__ import annotations

import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


class RetainedRotatingFileHandler(RotatingFileHandler):
    """Rotate by size, with both a backup-count cap and an age limit."""

    def __init__(
        self, filename: str | Path, *, max_bytes: int, backup_count: int,
        max_age_days: float, encoding: str = "utf-8",
    ) -> None:
        if max_bytes <= 0 or backup_count <= 0 or max_age_days <= 0:
            raise ValueError("Log size, backup count, and retention days must be positive")
        super().__init__(
            filename, maxBytes=max_bytes, backupCount=backup_count, encoding=encoding,
        )
        self.max_age_seconds = max_age_days * 86400
        self._next_prune = 0.0
        self.prune_old_backups()

    def prune_old_backups(self) -> None:
        """Remove only numbered backups of this log, never the active file."""
        active = Path(self.baseFilename)
        cutoff = time.time() - self.max_age_seconds
        for backup in active.parent.glob(f"{active.name}.*"):
            suffix = backup.name[len(active.name) + 1:]
            if not suffix.isdecimal():
                continue
            try:
                if backup.stat().st_mtime < cutoff:
                    backup.unlink()
            except FileNotFoundError:
                pass
        self._next_prune = time.monotonic() + 3600

    def doRollover(self) -> None:
        super().doRollover()
        self.prune_old_backups()

    def emit(self, record) -> None:
        super().emit(record)
        if time.monotonic() >= self._next_prune:
            self.prune_old_backups()
