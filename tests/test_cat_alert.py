"""Cat alerts must stay one-shot and refuse weak or repeated detections."""

import threading
from types import SimpleNamespace

from PIL import Image

from cat_alert import (
    CatArrivalGate, CatCandidate, CatIdentifier, CatVerdict,
    OneShotCatAnnouncer, resolve_cat_identity,
)


def cat_event(object_id="cat-1", kind="new", score=0.9, box=None):
    return {
        "type": kind,
        "after": {
            "camera": "desk_camera", "label": "cat", "id": object_id,
            "top_score": score, "box": box or [100, 100, 260, 260],
            "end_time": 1 if kind == "end" else None,
        },
    }


def test_cat_gate_one_alert_per_encounter_with_cooldown():
    gate = CatArrivalGate("desk_camera", absence_seconds=30, cooldown_seconds=90)
    candidate = gate.observe(cat_event(), now=0)
    assert candidate.object_id == "cat-1"
    assert gate.observe(cat_event(kind="update"), now=1) is None
    gate.complete("cat-1", announced=True, now=2)
    assert gate.observe(cat_event(kind="update"), now=4) is None
    gate.observe(cat_event(kind="end"), now=10)
    assert gate.observe(cat_event("cat-2"), now=15) is None
    gate.observe(cat_event("cat-2", kind="end"), now=20)
    assert gate.observe(cat_event("cat-3"), now=120).object_id == "cat-3"


def test_cat_gate_retries_uncertain_view_without_announcing_any_cat():
    gate = CatArrivalGate("desk_camera")
    assert gate.observe(cat_event(score=0.4), now=0) is None
    assert gate.observe(cat_event(box=[1, 1, 20, 20]), now=1) is None
    assert gate.observe(cat_event(), now=2).object_id == "cat-1"
    gate.complete("cat-1", announced=False, now=3)
    assert gate.observe(cat_event(kind="update"), now=4) is None
    assert gate.observe(cat_event(kind="update"), now=6).object_id == "cat-1"
    gate.complete("cat-1", announced=False, now=7)
    assert gate.observe(cat_event(kind="update"), now=10).object_id == "cat-1"
    gate.complete("cat-1", announced=False, now=11)
    assert gate.observe(cat_event(kind="update"), now=20) is None


def test_frigate_classification_only_updates_matching_active_cat_track():
    gate = CatArrivalGate("desk_camera")
    assert gate.observe(cat_event(), now=0) is not None
    update = {
        "type": "classification", "camera": "desk_camera", "id": "cat-1",
        "model": "household_cats", "sub_label": "chichi", "score": 0.93,
    }
    for change in ({"model": "other_model"}, {"camera": "other_camera"},
                   {"id": "other-cat"}, {"score": 0.79}):
        gate.observe_classification({**update, **change})
        assert gate.classifier_result("cat-1") is None
    gate.observe_classification(update)
    assert gate.classifier_result("cat-1") == ("chichi", 0.93)
    assert gate.classifier_event("cat-1").is_set()
    gate.observe(cat_event(kind="end"), now=1)
    assert gate.classifier_result("cat-1") is None


def test_cat_identity_conflicts_and_unknowns_use_generic_alert():
    assert resolve_cat_identity("tabo", ("tabo", 0.9)) == "tabo"
    assert resolve_cat_identity("chichi", ("chichi", 0.9)) == "chichi"
    assert resolve_cat_identity("tabo", ("chichi", 0.9)) == "cat"
    assert resolve_cat_identity("chichi", ("tabo", 0.9)) == "cat"
    assert resolve_cat_identity("chichi", None) == "chichi"
    assert resolve_cat_identity("cat", ("tabo", 0.9)) == "cat"
    assert resolve_cat_identity("none", ("chichi", 0.9)) == "none"


def test_visual_check_compares_both_cats_and_requires_clear_identity(tmp_path, monkeypatch):
    tabo_dir = tmp_path / "Tabo"
    chichi_dir = tmp_path / "chichi"
    tabo_dir.mkdir()
    chichi_dir.mkdir()
    for index in range(2):
        Image.new("RGB", (160, 160), "brown").save(tabo_dir / f"{index}.jpg")
    Image.new("RGB", (160, 160), "black").save(chichi_dir / "1.jpg")
    checker = CatIdentifier(None, tabo_dir, "test-model", chichi_dir)
    verdict = [CatVerdict(match="chichi", cat_visible=True, clear_view=True, reason="clear")]
    captured = []

    def fake_run(_agent, messages, **_kwargs):
        captured.append(messages[0]["content"])
        return SimpleNamespace(final_output=verdict[0])

    monkeypatch.setattr("cat_alert.Runner.run_sync", fake_run)
    candidate = CatCandidate("cat-1", (0, 0, 150, 150))
    frame = Image.new("RGB", (200, 200), "black")
    assert checker.classify(candidate, frame) == "chichi"
    labels = [item["text"] for item in captured[0] if item["type"] == "input_text"]
    assert any(label.startswith("Tabo 参考照") for label in labels)
    assert any(label.startswith("chichi 参考照") for label in labels)
    verdict[0] = CatVerdict(match="chichi", cat_visible=True, clear_view=False, reason="partial")
    assert checker.classify(candidate, frame) == "cat"
    verdict[0] = CatVerdict(match="chichi", cat_visible=False, clear_view=True, reason="false positive")
    assert checker.classify(candidate, frame) == "none"


def test_three_distinct_one_shot_alerts_are_prepared_once(monkeypatch):
    spoken = []

    def create(**kwargs):
        spoken.append(kwargs["input"])
        return SimpleNamespace(content=kwargs["input"].encode())

    monkeypatch.setattr(
        "cat_alert.OpenAI",
        lambda **_kwargs: SimpleNamespace(audio=SimpleNamespace(
            speech=SimpleNamespace(create=create))),
    )
    announcer = OneShotCatAnnouncer(threading.Event())
    assert announcer.prepare("tabo") == b"Tabo is here."
    assert announcer.prepare("chichi") == b"Chichi is here."
    assert announcer.prepare("cat") == b"A cat is here."
    announcer.prepare("chichi")
    assert spoken == ["Tabo is here.", "Chichi is here.", "A cat is here."]
