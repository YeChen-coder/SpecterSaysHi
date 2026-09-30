from echo import SpeakerReference


def test_speaker_reference_pairs_pcm_and_pads_silence():
    reference = SpeakerReference(max_bytes=8)
    reference.push(b"\x01\x00\x02\x00")
    assert reference.take(6) == b"\x01\x00\x02\x00\x00\x00"
    assert reference.take(2) == b"\x00\x00"


def test_interrupted_reference_is_discarded():
    reference = SpeakerReference(max_bytes=8)
    reference.push(b"\x01\x00\x02\x00\x03\x00\x04\x00\x05\x00")
    assert reference.take(4) == b"\x02\x00\x03\x00"
    reference.clear()
    assert reference.take(4) == bytes(4)
