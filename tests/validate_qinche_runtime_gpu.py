"""Run in the QinChe image: compare live predictions to the approved render cache."""
import json
import time
from pathlib import Path

import cv2
import numpy as np
import soundfile as sf
from engine import QinCheEngine
from realtime_session import RealtimeLoopSession


def main():
    engine=QinCheEngine()
    reference=np.load('/assets/output/native_face/qinche_speech_12_v14_predictions.npy')
    audio,sr=sf.read('/assets/data/qinche_speech_12_v14/audio.wav',dtype='float32')
    assert sr==16000
    audio=audio[:160000]
    predictions=[]
    times=[]
    original_render=engine.render
    def render(window):
        index=int(engine.source_indices[engine.cursor])
        before=engine.frames[index].copy()
        image,pred=original_render(window)
        predictions.append(pred)
        times.append(engine.composite_last_ms)
        x1,y1,x2,y2=map(int,engine.bboxes[index])
        protected=engine.prepared[index]['alpha'][...,0]==0
        assert np.array_equal(image[y1:y2,x1:x2][protected],before[y1:y2,x1:x2][protected])
        before[y1:y2,x1:x2]=image[y1:y2,x1:x2]
        assert np.array_equal(before,image)
        return image,pred
    engine.render=render
    session=RealtimeLoopSession(engine,'fast',50,'model')
    for offset in range(0,len(audio),1600):list(session.push(audio[offset:offset+1600]))
    list(session.push([],final=True))
    actual=np.stack(predictions)
    assert actual.shape==reference.shape
    delta=np.abs(actual.astype(int)-reference.astype(int))
    assert delta.max()<=1, f'Prediction diverged from approved cache: {delta.max()}'
    assert engine.cursor==300
    for cursor,expected in [(249,249),(250,250),(251,249),(499,1),(500,0)]:
        engine.begin(cursor)
        assert engine.matcher.field is None
        assert np.array_equal(engine.idle_frame(cursor),engine.frames[expected])
        original_render(np.zeros((20,2,1024),dtype=np.float32))
        assert engine.cursor==(cursor+1)%500
    out=Path('/checks/output');out.mkdir(exist_ok=True)
    report=dict(approved_prediction_frames=len(actual),prediction_max_error=int(delta.max()),
        prediction_mean_error=float(delta.mean()),native_preservation_checks=len(actual),
        timeline_turns_and_wrap=True,field_reset=True,composite_median_ms=float(np.median(times)),
        composite_p95_ms=float(np.percentile(times,95)))
    (out/'gpu_report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))


if __name__=='__main__':main()
