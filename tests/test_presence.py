from presence import PresenceGate


def event(object_id, name=None, area=50000, kind="new"):
    side = int(area**0.5)
    return {
        "type": kind,
        "after": {
            "id": object_id,
            "camera": "desk_camera",
            "label": "person",
            "box": [0, 0, side, side],
            "sub_label": [name, 0.96] if name else None,
            "end_time": 1 if kind == "end" else None,
        },
    }


def test_greets_only_named_person_once_per_encounter():
    gate = PresenceGate("Alice", cooldown_seconds=0, absence_seconds=20)
    assert not gate.observe("frigate/events", event("roommate", "Bob"), now=0)
    assert not gate.observe("frigate/events", event("a"), now=1)
    assert gate.observe(
        "frigate/tracked_object_update",
        {"type": "face", "id": "a", "camera": "desk_camera", "name": "Alice", "score": 0.96},
        now=2,
    )
    assert not gate.observe("frigate/events", event("a", "Alice"), now=3)
    assert not gate.observe("frigate/events", event("a", "Alice", kind="end"), now=4)
    assert gate.observe("frigate/events", event("b", "Alice"), now=25)


def test_approach_waits_until_person_is_near():
    gate = PresenceGate("Alice", min_person_area=40000)
    assert not gate.observe("frigate/events", event("a", "Alice", 10000), now=0)
    assert gate.observe("frigate/events", event("a", "Alice", 90000, "update"), now=2)


def test_rejects_low_confidence_and_other_camera():
    gate = PresenceGate("Alice")
    assert not gate.observe("frigate/events", event("a"), now=0)
    assert not gate.observe(
        "frigate/tracked_object_update",
        {"type": "face", "id": "a", "camera": "desk_camera", "name": "Alice", "score": 0.5},
        now=1,
    )
    assert not gate.observe(
        "frigate/tracked_object_update",
        {"type": "face", "id": "a", "camera": "other", "name": "Alice", "score": 0.99},
        now=2,
    )


def test_stationary_person_does_not_become_a_new_encounter_without_end():
    gate = PresenceGate("Alice", cooldown_seconds=0, absence_seconds=20)
    assert gate.observe("frigate/events", event("a", "Alice"), now=0)
    assert not gate.observe("frigate/events", event("a", "Alice", kind="update"), now=120)
    assert not gate.observe("frigate/events", event("b", "Alice"), now=121)


def test_roommate_leaving_does_not_delay_owner_return():
    gate = PresenceGate("Alice", cooldown_seconds=0, absence_seconds=20)
    assert gate.observe("frigate/events", event("owner", "Alice"), now=0)
    assert not gate.observe("frigate/events", event("roommate", "Bob"), now=1)
    assert not gate.observe("frigate/events", event("owner", "Alice", kind="end"), now=2)
    assert not gate.observe("frigate/events", event("roommate", "Bob", kind="end"), now=15)
    assert gate.observe("frigate/events", event("owner-returned", "Alice"), now=23)
