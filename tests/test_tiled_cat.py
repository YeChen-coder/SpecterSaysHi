"""The local scan must notify on arrival, never merely on startup."""

from cat_alert import CatCandidate
from tiled_cat import TiledCatArrivalGate


CAT = CatCandidate("local-cat-scan", (800, 350, 1200, 700))


def test_existing_cat_at_startup_does_not_trigger():
    gate = TiledCatArrivalGate(absence_seconds=30, cooldown_seconds=90)
    assert gate.observe(CAT, now=0) is None
    assert gate.observe(CAT, now=2) is None
    assert gate.observe(None, now=10) is None
    assert gate.observe(None, now=41) is None
    assert gate.observe(CAT, now=43) is None
    assert gate.observe(CAT, now=45) == CAT
    gate.mark_alert(now=45)
    assert gate.observe(CAT, now=47) is None


def test_short_tracking_gap_and_cooldown_do_not_repeat_alert():
    gate = TiledCatArrivalGate(absence_seconds=30, cooldown_seconds=90)
    gate.observe(None, now=0)
    gate.observe(None, now=31)
    assert gate.observe(CAT, now=33) is None
    assert gate.observe(CAT, now=35) == CAT
    gate.mark_alert(now=35)
    gate.observe(None, now=40)
    assert gate.observe(CAT, now=42) is None
    assert gate.observe(CAT, now=44) is None
    gate.observe(None, now=50)
    gate.observe(None, now=81)
    assert gate.observe(CAT, now=83) is None
    assert gate.observe(CAT, now=85) is None  # Still inside 90-second cooldown.
