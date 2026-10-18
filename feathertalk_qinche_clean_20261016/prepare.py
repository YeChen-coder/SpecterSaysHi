"""Fresh sources, original Gentleman JPEG/crop/landmark and audio protocol."""
import os,sys,json,subprocess,time,hashlib
from pathlib import Path
import cv2,numpy as np,torch,soundfile as sf
from face_utils import compute_face_bbox,crop_face,extract_inner
from dataset_mouth_roi import MouthRoiConfig,mouth_mask_from_landmarks
from data_utils.feather_hubert.feather_hubert import load_feather_hubert,get_feather_hubert_from_16k_speech

torch.set_num_threads(4);cv2.setNumThreads(1);torch.cuda.set_per_process_memory_fraction(.45)
os.chdir('/opt/FeatherTalk/data_utils');sys.path.insert(0,os.getcwd())
from get_landmark import Landmark
root=Path('/work/data');out=Path('/work/output');source=Path('/sources')
root.mkdir(exist_ok=True);out.mkdir(exist_ok=True)
det=Landmark();encoder=load_feather_hubert('/opt/FeatherTalk/feather_hubert.pth',torch.device('cuda'))
heldout=['qinche_speech_12_v14','qinche_speech_36_v14'];rows=[];start=time.perf_counter()
files=sorted(source.glob('qinche_speech_*_v14.mp4'));assert len(files)==24
framefile=root/'frame.jpg'
for file in files:
    name=file.stem;dest=root/name;dest.mkdir(exist_ok=True)
    normalized=root/'normalized.mkv'
    probe=json.loads(subprocess.check_output(['/usr/bin/ffprobe','-v','error','-show_streams','-of','json',str(file)]))
    video=next(s for s in probe['streams'] if s['codec_type']=='video');audio=next(s for s in probe['streams'] if s['codec_type']=='audio')
    assert abs(float(video.get('start_time',0))-float(audio.get('start_time',0)))<=.04
    subprocess.run(['/usr/bin/ffmpeg','-v','error','-y','-i',str(file),'-map','0:v:0','-map','0:a:0','-vf','fps=25','-c:v','libx264','-threads','2','-preset','fast','-crf','18','-c:a','pcm_s16le',str(normalized)],check=True)
    cap=cv2.VideoCapture(str(normalized));crops=[];masks=[];points=[]
    while True:
        ok,image=cap.read()
        if not ok:break
        # Gentleman originally trained from JPEG95 frames and ran detect(path).
        assert cv2.imwrite(str(framefile),image,[cv2.IMWRITE_JPEG_QUALITY,95])
        image=cv2.imread(str(framefile));pts,x,y=det.detect(str(framefile));pts=pts+np.array([x,y]);bbox=compute_face_bbox(pts)
        assert min(bbox)>=0 and bbox[2]<=image.shape[1] and bbox[3]<=image.shape[0]
        crops.append(extract_inner(crop_face(image,bbox)))
        masks.append(mouth_mask_from_landmarks(pts,MouthRoiConfig()).numpy().astype(np.uint8));points.append(pts)
    cap.release();n=len(crops);assert n>1
    raw=subprocess.check_output(['/usr/bin/ffmpeg','-v','error','-i',str(normalized),'-vn','-ac','1','-ar','16000','-f','f32le','-'])
    wave=np.frombuffer(raw,dtype='<f4').copy();sf.write(dest/'audio.wav',wave,16000)
    padded=np.pad(wave[:n*640+80],(0,max(0,n*640+80-len(wave))))
    with torch.inference_mode():features=get_feather_hubert_from_16k_speech(padded,encoder,torch.device('cuda'))[:n*2].numpy().reshape(n,2,1024)
    for key,arr in [('crops',crops),('masks',masks),('landmarks',points),('features',features)]:np.save(dest/(key+'.npy'),np.asarray(arr))
    row=dict(name=name,frames=n,split='validation' if name in heldout else 'train',sha256=hashlib.sha256(file.read_bytes()).hexdigest())
    rows.append(row);cv2.imwrite(str(out/(name+'_atlas.jpg')),np.concatenate([cv2.resize(crops[i],(144,144)) for i in np.linspace(0,n-1,6).astype(int)],axis=1))
    (out/'preparation_progress.json').write_text(json.dumps(dict(clips_done=len(rows),total_clips=len(files),last=row,elapsed_seconds=time.perf_counter()-start),indent=2))
    print('PREPARED',json.dumps(row),flush=True)
# Neutral input is decoded again from a supplied source, never an old cache.
neutral=source/'qinche_quiet_v14_loop.mp4';cap=cv2.VideoCapture(str(neutral));ok,image=cap.read();cap.release();assert ok
assert cv2.imwrite(str(root/'base.jpg'),image,[cv2.IMWRITE_JPEG_QUALITY,95])
pts,x,y=det.detect(str(root/'base.jpg'));np.savetxt(root/'base.lms',pts+np.array([x,y]),fmt='%d')
manifest=dict(character='QinChe',clips=rows,heldout=heldout,quiet_is_inference_only=True,neutral_source=neutral.name,neutral_sha256=hashlib.sha256(neutral.read_bytes()).hexdigest(),old_artifacts_used=False,preparation_seconds=time.perf_counter()-start)
(root/'manifest.json').write_text(json.dumps(manifest,indent=2));print('PREPARE_COMPLETE',flush=True)
