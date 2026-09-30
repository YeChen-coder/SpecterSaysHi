"""Long-reply history bounds and frame continuity, without loading GPU weights."""
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from feathertalk_lab.realtime_session import RealtimeLoopSession


class FakeEngine:
    count = 202

    def __init__(self):
        self.teeth = SimpleNamespace(records=[], last={"replaced": True})
        self.indices = []
        self.teeth_rows = []
        self.windows = []

    def begin(self, frame):
        self.cursor = frame % self.count

    def encode(self, pcm):
        return pcm[::640]

    def render(self, window):
        self.windows.append(window)
        self.indices.append(self.cursor)
        self.teeth_rows.append({})
        self.teeth.records.append({})
        self.cursor = (self.cursor + 1) % self.count
        return window, None

    def sync(self):
        pass


@pytest.mark.parametrize("mode,lookahead", [("fast", 0), ("balanced", 10), ("quality", 25)])
def test_long_reply_keeps_frame_order_and_bounded_history(monkeypatch, mode, lookahead):
    monkeypatch.setitem(sys.modules, "stream_core", SimpleNamespace(MODES={mode: {"lookahead": lookahead}}))
    monkeypatch.setitem(sys.modules, "face_utils", SimpleNamespace(
        gather_audio_window=lambda features, index: SimpleNamespace(numpy=lambda: features[index])))
    engine = FakeEngine()
    session = RealtimeLoopSession(engine, mode, 190, "new")
    # Each audio frame carries its absolute index so trimming cannot hide a timeline error.
    pcm = np.repeat(np.arange(1125, dtype=np.float32), 640)  # 45 seconds, beyond lab's 30s limit
    frames = []
    for offset in range(0, len(pcm), 1600):
        frames.extend(session.push(pcm[offset:offset + 1600]))
        assert len(session.pcm) <= (101 + lookahead) * 640 + 1600
        assert not engine.indices and not engine.teeth_rows and not engine.teeth.records
    frames.extend(session.push([], final=True))
    assert len(frames) == 1125
    assert [index for index, _ in frames] == [(190 + i) % 202 for i in range(1125)]
    assert engine.windows == list(range(1125))
    assert session.teeth_restored_frames == 1125
