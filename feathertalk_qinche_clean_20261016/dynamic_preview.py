"""Fresh moving reference, unchanged epoch100, plain FeatherTalk compositing."""
import os,sys,json,time,subprocess,hashlib,gc
from pathlib import Path
import cv2,numpy as np,torch,soundfile as sf
from face_utils import compute_face_bbox,crop_face,extract_inner,mask_mouth,hwc_to_chw_tensor,gather_audio_window,reshape_audio_feat
from inference import paste_prediction
from stream_core import Engine,Session

torch.set_num_threads(4);cv2.setNumThreads(1);torch.cuda.set_per_process_memory_fraction(.45)
variant=os.getenv('DYNAMIC_VARIANT','');motion=variant in ['motion','motion-nod'];nod=variant=='motion-nod';natural=variant=='natural';model_idle=motion
suffix='_natural' if natural else '_motion_nod' if nod else '_motion' if motion else ''
data=Path('/work/data/dynamic_base'+suffix);out=Path('/work/output/dynamic'+suffix);data.mkdir(exist_ok=True);out.mkdir(exist_ok=True)
started=time.perf_counter();checkpoint=Path('/work/checkpoints/epoch_100.pth')
checkpoint_sha=hashlib.sha256(checkpoint.read_bytes()).hexdigest()
manifest=json.loads(Path('/work/data/manifest.json').read_text())
if natural:
    selection=json.loads(Path('/work/data/natural_selection.json').read_text());assert selection['qualified_for_preview']
    assert selection['source'] in ['idle_natural_20261017/natural_listening_01.mp4','idle_natural_20261017/natural_waiting_02.mp4']
    source=Path('/sources')/selection['source'];expected_sha=selection['sha256']
else:
    source=Path('/sources')/('qinche_speech_25.mp4' if nod else 'qinche_speech_25_v5.mp4' if motion else manifest['neutral_source'])
    expected_sha='c30081e74a27430b6771004a05e250c441b5e7444e1a5840628534d42d4f9708' if nod else 'a309f45df3866ba743d83a9b1a032f39f598730ef7a2334745cf98e11032e3b1' if motion else manifest['neutral_sha256']
source_sha=hashlib.sha256(source.read_bytes()).hexdigest()
assert source_sha==expected_sha
def status(**fields):
    p=out/'status.tmp';p.write_text(json.dumps(fields,indent=2));p.replace(out/'status.json')
status(state='running',stage='prepare')
os.chdir('/opt/FeatherTalk/data_utils');sys.path.insert(0,os.getcwd())
from get_landmark import Landmark
det=Landmark();probe=json.loads(subprocess.check_output(['/usr/bin/ffprobe','-v','error','-show_streams','-of','json',str(source)]))
v=next(s for s in probe['streams'] if s['codec_type']=='video');w,h=v['width'],v['height'];size=w*h*3
decoder=subprocess.Popen(['/usr/bin/ffmpeg','-v','error','-i',str(source),'-vf','fps=25','-threads','2','-f','rawvideo','-pix_fmt','bgr24','-'],stdout=subprocess.PIPE)
frames=[];points=[];boxes=[]
with torch.inference_mode():
    while True:
        raw=decoder.stdout.read(size)
        if not raw:break
        assert len(raw)==size;image=np.frombuffer(raw,np.uint8).reshape(h,w,3).copy()
        assert cv2.imwrite('/tmp/qinche_dynamic_frame.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,95])
        pts,x,y=det.detect('/tmp/qinche_dynamic_frame.jpg');pts=pts+np.array([x,y]);box=compute_face_bbox(pts)
        assert min(box)>=0 and box[2]<=w and box[3]<=h
        frames.append(image);points.append(pts);boxes.append(box)
assert decoder.wait()==0 and len(frames)>2
del det;gc.collect();torch.cuda.empty_cache()
frames=np.asarray(frames);points=np.asarray(points);boxes=np.asarray(boxes)
indices=np.concatenate([np.arange(len(frames)),np.arange(len(frames)-2,0,-1)])
assert np.abs(np.diff(np.concatenate([indices,[indices[0]]]))).max()==1
for key,arr in [('frames',frames),('landmarks',points),('bboxes',boxes),('source_indices',indices)]:np.save(data/(key+'.npy'),arr)

def writer(file,audio=None):
    command=['/usr/bin/ffmpeg','-y','-v','error','-f','rawvideo','-pix_fmt','bgr24','-s',f'{w}x{h}','-r','25','-i','-']
    if audio:command+=['-i',str(audio)]
    command+=['-c:v','libx264','-threads','2','-preset','fast','-crf','17','-pix_fmt','yuv420p']
    command+=['-c:a','aac','-shortest'] if audio else ['-an']
    command+=['-movflags','+faststart',str(out/file)]
    return subprocess.Popen(command,stdin=subprocess.PIPE)
class MovingEngine(Engine):
    def __init__(self):
        super().__init__(str(checkpoint));inputs=[];self.crops=[]
        for image,box in zip(frames,boxes):
            crop=crop_face(image,tuple(box));inner=extract_inner(crop);self.crops.append(crop)
            inputs.append(torch.cat([hwc_to_chw_tensor(inner.copy()),hwc_to_chw_tensor(mask_mouth(inner.copy()))],0))
        self.inputs=torch.stack(inputs).to(self.device);self.cursor=0;self.rendered_indices=[];self.checked_outside=0
    @torch.inference_mode()
    def render(self,window):
        i=int(indices[self.cursor%len(indices)]);image=frames[i].copy();box=tuple(boxes[i]);x1,y1,x2,y2=box
        p=self.visual(self.inputs[i:i+1],reshape_audio_feat(torch.from_numpy(window)).unsqueeze(0).to(self.device))[0]
        prediction=(p.cpu().numpy().transpose(1,2,0)*255).clip(0,255).astype(np.uint8)
        paste_prediction(image,prediction,self.crops[i].copy(),box,(x2-x1,y2-y1))
        if self.cursor%25==0:
            assert np.array_equal(image[:y1],frames[i,:y1]) and np.array_equal(image[y2:],frames[i,y2:])
            assert np.array_equal(image[y1:y2,:x1],frames[i,y1:y2,:x1]) and np.array_equal(image[y1:y2,x2:],frames[i,y1:y2,x2:])
            self.checked_outside+=1
        self.rendered_indices.append(i);self.cursor+=1
        return image,prediction
status(state='running',stage='render',source_frames=len(frames),loop_frames=len(indices))
engine=MovingEngine();clips=[]
silent_features=engine.encode(np.zeros(64080,np.float32));silent_window=gather_audio_window(silent_features,50).numpy()
def idle_frame(cursor):
    if not model_idle:return frames[indices[cursor%len(indices)]]
    engine.cursor=cursor;image,_=engine.render(silent_window);return image
process=writer('idle_loop.mp4')
for cursor in range(len(indices)):process.stdin.write(idle_frame(cursor).tobytes())
process.stdin.close();assert process.wait()==0
for name in ['qinche_speech_03_v14','qinche_speech_12_v14','qinche_speech_36_v14']:
    pcm,sr=sf.read(Path('/work/data')/name/'audio.wav',dtype='float32');assert sr==16000;pcm=pcm[:160000];assert len(pcm)==160000
    audio=out/(name+'.wav');sf.write(audio,np.concatenate([np.zeros(32000,np.float32),pcm,np.zeros(64000,np.float32)]),sr)
    features=engine.encode(np.pad(pcm,(0,80)))
    for mode in ['offline','stream']:
        process=writer(name+'_'+mode+'.mp4',audio);count=0
        for cursor in range(50):process.stdin.write(idle_frame(cursor).tobytes());count+=1
        engine.cursor=50;engine.rendered_indices=[]
        if mode=='offline':
            for j in range(250):
                image,_=engine.render(gather_audio_window(features,j).numpy());process.stdin.write(image.tobytes());count+=1
        else:
            session=Session(engine,'fast')
            for offset in range(0,len(pcm),1600):
                for j,image,pred in session.push(pcm[offset:offset+1600]):process.stdin.write(image.tobytes());count+=1
            for j,image,pred in session.push([],True):process.stdin.write(image.tobytes());count+=1
        assert engine.rendered_indices==indices[np.arange(50,300)%len(indices)].tolist()
        for cursor in range(300,400):process.stdin.write(idle_frame(cursor).tobytes());count+=1
        process.stdin.close();assert process.wait()==0 and count==400
        cap=cv2.VideoCapture(str(out/(name+'_'+mode+'.mp4')));cap.set(cv2.CAP_PROP_POS_FRAMES,75);ok,poster=cap.read();cap.release();assert ok
        cv2.imwrite(str(out/(name+'_'+mode+'.jpg')),poster)
        clips.append(dict(name=name,mode=mode,frames=count,speech_frames=250,speech_start_seconds=2,speech_end_seconds=12,background_continuous=True))
        print('EXPORTED',name,mode,flush=True)
assert hashlib.sha256(checkpoint.read_bytes()).hexdigest()==checkpoint_sha
for row in clips:
    file=out/(row['name']+'_'+row['mode']+'.mp4')
    p=json.loads(subprocess.check_output(['/usr/bin/ffprobe','-v','error','-count_frames','-show_streams','-of','json',str(file)]))
    video=next(s for s in p['streams'] if s['codec_type']=='video');audio=next(s for s in p['streams'] if s['codec_type']=='audio')
    assert int(video['nb_read_frames'])==400 and abs(float(video['duration'])-float(audio['duration']))<.1
report=dict(state='complete',checkpoint='epoch_100.pth',checkpoint_sha256=checkpoint_sha,checkpoint_unchanged=True,source=source.name,source_sha256=source_sha,source_frames=len(frames),loop_frames=len(indices),loop_seconds=len(indices)/25,loop='forward then reverse; no duplicated endpoints',landmark_smoothing=False,old_qinche_artifacts_used=False,postprocessing=False,idle_mouth='same raw model driven by encoded digital silence' if model_idle else 'native quiet source',outside_face_box_checks=engine.checked_outside,clips=clips,elapsed_seconds=time.perf_counter()-started,note='Moving-reference inference with original paste_prediction. Incremental 100 ms inputs exported without wall-clock pacing; not a latency benchmark. No new model training or appearance corrections.')
(out/'report.json').write_text(json.dumps(report,indent=2));status(state='complete',stage='complete',loop_seconds=len(indices)/25);print(json.dumps(report),flush=True)
