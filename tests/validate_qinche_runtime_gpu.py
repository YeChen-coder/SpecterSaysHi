"""Run in the QinChe image; validate selected windows, transport and native pixels."""
import hashlib,json
from pathlib import Path
import cv2,numpy as np,soundfile as sf,torch
from engine import QinCheEngine
from realtime_session import RealtimeLoopSession
from native_face_match import NativeFaceMatch
from face_utils import reshape_audio_feat
from frame_encoding import encode_frame,frame_metadata

def main():
    e=QinCheEngine();audio,sr=sf.read('/assets/data/test_speech.wav',dtype='float32');assert sr==16000
    windows=[];render=e.render
    def capture(window):windows.append(window.copy());return None,None
    e.render=capture;s=RealtimeLoopSession(e,'fast',50,'model')
    for offset in range(0,160000,1600):list(s.push(audio[offset:offset+1600]))
    list(s.push([],final=True));assert len(windows)==250
    sha=hashlib.sha256(np.asarray(windows).tobytes()).hexdigest()
    assert sha=='611df8dc1c51c0519450975644c9fbc9849eaeb6cee22e8d7fa22382552f07ad'
    e.render=render;e.begin(50);reference=NativeFaceMatch();prediction_errors=[];composite_errors=[]
    with torch.inference_mode():
        for n,window in enumerate(windows):
            cursor=50+n;source=int(e.source_indices[cursor]);image,pred=render(window)
            inputs=torch.stack([e.inputs[source],e.inputs[source]])
            feat=reshape_audio_feat(torch.from_numpy(window)).unsqueeze(0).expand(2,-1,-1).to(e.device)
            expected=(e.visual(inputs,feat)[1].cpu().numpy().transpose(1,2,0)*255).clip(0,255).astype(np.uint8)
            prediction_errors.append(int(abs(expected.astype(int)-pred.astype(int)).max()))
            roi=reference.apply(expected,e.references[source],e.prepared[source]);x1,y1,x2,y2=map(int,e.bboxes[source])
            composite_errors.append(int(abs(roi.astype(int)-image[y1:y2,x1:x2].astype(int)).max()))
            decoded=cv2.imdecode(np.frombuffer(encode_frame(e,cursor,image),np.uint8),cv2.IMREAD_UNCHANGED)
            np.testing.assert_array_equal(decoded[:,:,:3],image[y1:y2,x1:x2])
            support=e.prepared[source]['alpha'][:,:,0]>0
            np.testing.assert_array_equal(decoded[:,:,3],np.where(support,255,0))
            np.testing.assert_array_equal(image[y1:y2,x1:x2][~support],e.frames[source][y1:y2,x1:x2][~support])
            check=e.frames[source].copy();check[y1:y2,x1:x2]=image[y1:y2,x1:x2];np.testing.assert_array_equal(check,image)
            assert frame_metadata(e,cursor)['bbox']==e.bboxes[source].tolist()
    assert max(prediction_errors)<=2 and max(composite_errors)<=2
    for cursor,index in [(249,249),(250,250),(251,249),(499,1),(500,0)]:
        e.begin(cursor);assert e.matcher.field is None
        np.testing.assert_array_equal(e.idle_frame(cursor),e.frames[index])
    report={'passed':True,'selected_epoch':115,'audio_windows_sha256':sha,'approved_windows_identical':True,
            'frames':250,'prediction_max_error_batch1_vs_batch2':max(prediction_errors),'composite_max_error':max(composite_errors),
            'png_rgb_and_alpha_checks':250,'native_preservation_checks':250,'timeline_and_field_reset':True}
    Path('/checks/output').mkdir(exist_ok=True)
    Path('/checks/output/gpu_report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
if __name__=='__main__':main()
