"""Small log slices and age-based retention."""

import logging
import os
import time

from log_retention import RetainedRotatingFileHandler


def test_log_slices_are_small_and_backup_count_is_bounded(tmp_path):
    active = tmp_path / "specter.log"
    handler = RetainedRotatingFileHandler(
        active, max_bytes=100, backup_count=3, max_age_days=7,
    )
    logger = logging.getLogger(f"test_log_retention.{id(tmp_path)}")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        for number in range(30):
            logger.info("event %02d %s", number, "x" * 20)
    finally:
        logger.removeHandler(handler)
        handler.close()

    slices = sorted(tmp_path.glob("specter.log*"))
    assert {path.name for path in slices} == {
        "specter.log", "specter.log.1", "specter.log.2", "specter.log.3",
    }
    assert all(path.stat().st_size <= 100 for path in slices)
    assert "event 29" in active.read_text(encoding="utf-8")


def test_expired_backup_is_removed_without_touching_active_or_other_files(tmp_path):
    active = tmp_path / "specter.log"
    active.write_text("current\n", encoding="utf-8")
    expired = tmp_path / "specter.log.1"
    expired.write_text("old\n", encoding="utf-8")
    unrelated = tmp_path / "specter.log.notes"
    unrelated.write_text("keep\n", encoding="utf-8")
    old = time.time() - 8 * 86400
    os.utime(expired, (old, old))
    os.utime(active, (old, old))

    handler = RetainedRotatingFileHandler(
        active, max_bytes=100, backup_count=3, max_age_days=7,
    )
    handler.close()

    assert not expired.exists()
    assert active.read_text(encoding="utf-8") == "current\n"
    assert unrelated.read_text(encoding="utf-8") == "keep\n"
