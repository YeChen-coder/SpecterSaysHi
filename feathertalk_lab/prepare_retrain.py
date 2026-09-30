"""Prepare supplied clips without writing thousands of full-resolution frames."""
import os, sys, json, time, subprocess, hashlib
from pathlib import Path
import cv2, numpy as np, torch, soundfile as sf
from face_utils import compute_face_bbox, crop_face, extract_inner
from dataset_mouth_roi import MouthRoiConfig, mouth_mask_from_landmarks
sys.path.insert(0, '/opt/FeatherTalk/data_utils')
from feather_hubert.feather_hubert import load_feather_hubert, get_feather_hubert_from_16k_speech

torch.set_num_threads(4)
torch.cuda.set_per_process_memory_fraction(.45)
os.chdir('/opt/FeatherTalk/data_utils')
from get_landmark import Landmark, face_det
det = Landmark()
encoder = load_feather_hubert('/opt/FeatherTalk/feather_hubert.pth', torch.device('cuda'))
root = Path('/work/data/retrain_20260929'); root.mkdir(exist_ok=True)
out = Path('/work/output/retrain_20260929'); out.mkdir(exist_ok=True)
sources = Path('/work/data/new_training_sources/provided_20260929')
manifest = []; started = time.perf_counter()

@torch.inference_mode()
def landmarks(img):
    faces, boxes, _, _ = face_det(img, det.det_net)
    if not faces: raise ValueError('No face detected')
    face = faces[0]; h, w = face.shape[:2]
    tensor = torch.from_numpy((cv2.resize(face, (192,192)).astype(np.float32)/255).transpose(2,0,1))[None].to(det.device)
    pts = (det.pfld_backbone(tensor)[0].cpu().numpy()+det.mean_face).reshape(-1,2)
    pts[:,0] *= w; pts[:,1] *= h
    return pts.astype(np.int32)+np.asarray(boxes[0][:2])

def save_clip(name, crops, masks, points, features, metadata):
    path = root/name; path.mkdir(exist_ok=True)
    for key, arr in [('crops',crops),('masks',masks),('landmarks',points),('features',features)]:
        np.save(path/(key+'.npy'), np.asarray(arr))
    row = dict(name=name, frames=len(crops), seconds=len(crops)/25, **metadata)
    (path/'metadata.json').write_text(json.dumps(row,indent=2))
    manifest.append(row)
    # Eight equally spaced frames retain pose and tooth variation for review.
    thumbs=[cv2.resize(crops[i],(180,180)) for i in np.linspace(0,len(crops)-1,8).astype(int)]
    atlas=np.concatenate(thumbs,axis=1)
    cv2.putText(atlas,name,(8,20),cv2.FONT_HERSHEY_SIMPLEX,.55,(0,255,255),1)
    cv2.imwrite(str(out/(name+'_atlas.jpg')),atlas)
    print(json.dumps(row),flush=True)

# Keep the original clips available for rehearsal; their audio windows stay separate.
old=Path('/work/data/person')
features=np.load(old/'aud_hu.npy')
for row in json.loads((old/'clip_ranges.json').read_text())['ranges']:
    name='original_'+row['clip']; crops=[]; masks=[]; points=[]
    for i in range(row['start'],row['end']):
        img=cv2.imread(str(old/'full_body_img'/f'{i}.jpg'))
        pts=np.loadtxt(old/'landmarks'/f'{i}.lms').astype(np.int32)
        crops.append(extract_inner(crop_face(img,compute_face_bbox(pts))))
        masks.append(mouth_mask_from_landmarks(pts,MouthRoiConfig()).numpy().astype(np.uint8))
        points.append(pts)
    save_clip(name,crops,masks,points,features[row['start']:row['end']],dict(split='train',origin='previous_dataset'))

for source in sorted(sources.glob('*.mp4')):
    name=source.stem
    probe=json.loads(subprocess.check_output(['/usr/bin/ffprobe','-v','error','-show_streams','-of','json',str(source)]))
    video=next(s for s in probe['streams'] if s['codec_type']=='video' and not s.get('disposition',{}).get('attached_pic'))
    audio=next(s for s in probe['streams'] if s['codec_type']=='audio')
    vstart=float(video.get('start_time',0)); astart=float(audio.get('start_time',0))
    if abs(vstart-astart)>0.04: raise ValueError(f'{name}: verify audio/video timestamp offset before training')
    width,height=video['width'],video['height']; size=width*height*3
    proc=subprocess.Popen(['/usr/bin/ffmpeg','-v','error','-i',str(source),'-map','0:v:0','-vf','fps=25','-threads','2','-f','rawvideo','-pix_fmt','bgr24','-'],stdout=subprocess.PIPE)
    crops=[]; masks=[]; points=[]
    while True:
        raw=proc.stdout.read(size)
        if not raw: break
        if len(raw)!=size: raise ValueError('Incomplete raw frame')
        img=np.frombuffer(raw,np.uint8).reshape(height,width,3)
        pts=landmarks(img); bbox=compute_face_bbox(pts)
        if bbox[2]<=bbox[0] or min(bbox)<0 or bbox[2]>width or bbox[3]>height: raise ValueError(f'{name}: invalid crop {bbox}')
        crops.append(extract_inner(crop_face(img,bbox)))
        masks.append(mouth_mask_from_landmarks(pts,MouthRoiConfig()).numpy().astype(np.uint8)); points.append(pts)
    if proc.wait()!=0: raise RuntimeError('Video decode failed')
    raw=subprocess.check_output(['/usr/bin/ffmpeg','-v','error','-i',str(source),'-map','0:a:0','-ac','1','-ar','16000','-f','f32le','-'])
    wave=np.frombuffer(raw,dtype='<f4').copy(); count=len(crops)
    sf.write(str(root/(name+'.wav')),wave,16000)
    energy=np.sqrt(np.mean(np.pad(wave,(0,(-len(wave))%640)).reshape(-1,640)**2,axis=1))
    padded=np.pad(wave[:count*640+80],(0,max(0,count*640+80-len(wave))))
    feat=get_feather_hubert_from_16k_speech(padded,encoder,torch.device('cuda'))[:count*2].numpy().reshape(count,2,1024)
    save_clip(name,crops,masks,points,feat,dict(split='validation' if name.endswith('_11') else 'train',origin='provided_20260929',sha256=hashlib.sha256(source.read_bytes()).hexdigest(),video_start=vstart,audio_start=astart,audio_seconds=len(wave)/16000,active_audio_fraction=float(np.mean(energy>.015)),source_video_fps=video['r_frame_rate']))

(root/'manifest.json').write_text(json.dumps(dict(clips=manifest,prepare_seconds=time.perf_counter()-started),indent=2))
print('PREPARE_COMPLETE',time.perf_counter()-started,flush=True)
