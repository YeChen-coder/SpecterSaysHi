"""Matched comparisons and isolated streaming benchmarks for detail balance."""
from pathlib import Path
import cv2,numpy as np,json,time,subprocess,soundfile as sf
from loop_core import LoopEngine,LoopSession
from detail_balance import DetailBalance
from face_utils import gather_audio_window
out=Path('/work/output/detail_balance_20260930');out.mkdir(exist_ok=True)
engine=LoopEngine();filters=DetailBalance(engine)
variants=('original','speech_sharp','balanced','model_idle')
idle_orig=engine.frames
idle_soft=np.empty_like(idle_orig);idle_model=np.empty_like(idle_orig)
idle_times=[];sharp_times=[]
engine.begin(0);engine.teeth_mode='new'
window=gather_audio_window(np.zeros((50,2,1024),np.float32),25).numpy()
for i in range(engine.count):
    idle_soft[i],_=filters.process(idle_orig[i].copy(),i,'soft')
    # Audio features encode silence, using the same trained generator.
    begin=time.perf_counter();f,_=engine.render(window);engine.sync()
    f,_=filters.process(f,i,'sharp')
    idle_model[i]=idle_orig[i]
    x1,y1,x2,y2=map(int,engine.bboxes[i]);original=idle_model[i,y1:y2,x1:x2]
    alpha=filters.masks[i]*np.clip((np.linspace(0,1,y2-y1)[:,None]-.55)/.12,0,1)
    mixed=np.rint(original*(1-alpha[...,None])+f[y1:y2,x1:x2]*alpha[...,None]).clip(0,255).astype(np.uint8)
    selected=alpha>0;original[selected]=mixed[selected]
    idle_times.append((time.perf_counter()-begin)*1000)
    if i%50==0:print('PRECOMPUTE_IDLE',i,flush=True)
np.save(out/'idle_model_frames.npy',idle_model)
def encode(path,frames,fps=25,audio=None):
    h,w=frames[0].shape[:2]
    cmd=['/usr/bin/ffmpeg','-hide_banner','-loglevel','error','-y','-f','rawvideo','-pix_fmt','bgr24','-s',f'{w}x{h}','-r',str(fps),'-i','-']
    if audio:cmd+=['-i',audio,'-map','0:v','-map','1:a','-c:a','aac']
    else:cmd+=['-an']
    cmd+=['-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',str(path)]
    p=subprocess.Popen(cmd,stdin=subprocess.PIPE)
    for f in frames:p.stdin.write(f.tobytes())
    p.stdin.close()
    if p.wait()!=0:raise RuntimeError('ffmpeg')
for name,frames in [('soft',idle_soft),('model',idle_model)]:encode(out/f'idle_{name}.mp4',frames)

pcm,sr=sf.read('/work/data/speech.wav',dtype='float32');assert sr==16000
session=LoopSession(engine,'fast',60,teeth='new');speech=[];enhanced=[];indices=[]
for offset in range(0,len(pcm)+1600,1600):
    final=offset>=len(pcm)
    for idx,f,pred in session.iter_push(pcm[offset:offset+1600] if not final else [],final):
        base=session.rows[idx]['base_frame'];speech.append(f.copy());indices.append(base)
        sharp,ms=filters.process(f.copy(),base);enhanced.append(sharp);sharp_times.append(ms)
    if final:break
print('SPEECH_GENERATED',len(speech),flush=True)
audio=out/'demo_audio.wav';sf.write(audio,np.concatenate([np.zeros(32000),pcm,np.zeros(32000)]),16000)
lead=[(60-50+i)%engine.count for i in range(50)]
tail=[(60+len(speech)+i)%engine.count for i in range(50)]
measurements={}
for variant in variants:
    idle=idle_soft if variant=='balanced' else idle_model if variant=='model_idle' else idle_orig
    speaking=speech if variant=='original' else enhanced
    full=[idle[i].copy() for i in lead]+[f.copy() for f in speaking]+[idle[i].copy() for i in tail]
    all_indices=lead+indices+tail
    # Same start/end fades for all variants, aligned with current UI behavior.
    for anchor,length in [(50,2),(50+len(speech),5)]:
        previous=full[anchor-1].copy()
        for j in range(length):full[anchor+j]=cv2.addWeighted(previous,1-(j+1)/length,full[anchor+j],(j+1)/length,0)
    encode(out/f'{variant}.mp4',full,audio=str(audio))
    faces=[]
    for f,i in zip(full,all_indices):
        x1,y1,x2,y2=engine.bboxes[i];faces.append(cv2.resize(f[y1:y2,x1:x2],(400,400)))
    encode(out/f'{variant}_face.mp4',faces,audio=str(audio))
    chosen=[0,49,52,125,275,50+len(speech)-1,50+len(speech)+5,len(full)-1]
    tiles=[]
    for k in chosen:
        f=cv2.resize(faces[k],(200,200));cv2.putText(f,f'{k/25:.2f}s',(4,18),cv2.FONT_HERSHEY_SIMPLEX,.5,(0,255,255),1);tiles.append(f)
    cv2.imwrite(str(out/f'{variant}_atlas.jpg'),np.concatenate(tiles,axis=1))
    # Quantify the same idle frames vs. closed-mouth speech frames with ROI high-pass energy.
    idle_energy=[];speak_energy=[]
    for j in range(len(speech)):
        i=indices[j];x1,y1,x2,y2=engine.bboxes[i]
        for target,frame in [(idle_energy,idle[i]),(speak_energy,speaking[j])]:
            patch=frame[y1:y2,x1:x2];g=cv2.cvtColor(patch,cv2.COLOR_BGR2GRAY).astype(np.float32)
            # Chin only, excludes moving lips and perimeter.
            g=g[int(g.shape[0]*.62):int(g.shape[0]*.88),int(g.shape[1]*.27):int(g.shape[1]*.73)]
            target.append(float(np.mean(np.abs(g-cv2.GaussianBlur(g,(0,0),1.05)))))
    measurements[variant]=dict(idle_chin_highpass_mean=float(np.mean(idle_energy)),speaking_chin_highpass_mean=float(np.mean(speak_energy)),absolute_energy_gap=abs(float(np.mean(idle_energy)-np.mean(speak_energy))))
    print('EXPORTED',variant,flush=True)

# CPU overhead over repeated identical inputs; no GPU timing mixed into this measure.
bench=[]
for repeat in range(8):
    for j in range(0,len(speech),3):
        _,ms=filters.process(speech[j].copy(),indices[j]);bench.append(ms)
report=dict(source_base_sha256=engine.manifest['sha256'],checkpoint='retrain_20260929/best.pth',teeth='new',fps=25,speech_frames=len(speech),idle_frames=engine.count,filters={'speech':'luma unsharp sigma 1.05 gain .70, noise floor .6, capped +/-8, feathered lower-face ROI','idle_soft':'luma Gaussian sigma 2.1, blend .80, same ROI','idle_model':'precomputed same visual model, zero audio features, same original reference frames; same sharp filter; composite chin only starting at 55% of ROI height, preserve idle lips'},cpu_filter_ms={'mean':float(np.mean(bench)),'p50':float(np.median(bench)),'p95':float(np.percentile(bench,95)),'p99':float(np.percentile(bench,99)),'n':len(bench)},idle_precompute_seconds=sum(idle_times)/1000,measurements=measurements)
(out/'offline_metrics.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
