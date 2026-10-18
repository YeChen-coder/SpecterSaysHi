"""Long streams must keep the timeline and a bounded four-second history."""
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from feathertalk_qinche_runtime.realtime_session import RealtimeLoopSession


@pytest.mark.parametrize('mode,lookahead', [('fast',0),('balanced',10),('quality',25)])
def test_long_stream_and_session_reset(monkeypatch, mode, lookahead):
    monkeypatch.setitem(sys.modules,'stream_core',SimpleNamespace(MODES={mode:{'lookahead':lookahead}}))
    monkeypatch.setitem(sys.modules,'face_utils',SimpleNamespace(
        gather_audio_window=lambda f,i:SimpleNamespace(numpy=lambda:f[i])))
    class Engine:
        def begin(self, frame):
            self.cursor=frame%500
            self.field=None
        def encode(self, pcm):return pcm[::640]
        def render(self, window):
            self.cursor=(self.cursor+1)%500
            self.field=window
            return window,None
        def sync(self):pass
    engine=Engine()
    session=RealtimeLoopSession(engine,mode,249,'model')
    samples=np.repeat(np.arange(1125,dtype=np.float32),640)
    output=[]
    for start in range(0,len(samples),1600):
        output.extend(session.push(samples[start:start+1600]))
        assert len(session.pcm)<=(101+lookahead)*640+1600
    output.extend(session.push([],final=True))
    assert [i for i,_ in output]==[(249+i)%500 for i in range(1125)]
    assert [v for _,v in output]==list(range(1125))
    RealtimeLoopSession(engine,mode,499,'model')
    assert engine.field is None and engine.cursor==499
    with pytest.raises(ValueError):RealtimeLoopSession(engine,mode,0,'new')
