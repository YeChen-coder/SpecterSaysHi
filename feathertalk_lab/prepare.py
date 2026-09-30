import sys, os, json, subprocess, time
from pathlib import Path
import cv2, numpy as np, torch
sys.path.insert(0,'/opt/FeatherTalk/data_utils')
from feather_hubert.feather_hubert import load_feather_hubert,get_feather_hubert_from_16k_speech
from face_utils import compute_face_bbox,crop_face,extract_inner
torch.set_num_threads(4)
os.chdir('/opt/FeatherTalk/data_utils')
from get_landmark import Landmark
det=Landmark()
enc=load_feather_hubert('/opt/FeatherTalk/feather_hubert.pth',torch.device('cuda'))
names=sys.argv[1:] or ['grok_15','grok_16','grok_17']
root=Path('/work/data/person'); (root/'full_body_img').mkdir(parents=True,exist_ok=True); (root/'landmarks').mkdir(exist_ok=True)
# Old prepared frames are retained only when the caller appends clips by rerunning
# with the same ordered source list. The active frame count is set by features.
index=0; parts=[];ranges=[];thumbs=[]
for name in names:
    source=Path('/work/data/sources')/(name+'.mp4')
    normalized=Path('/work/data')/(name+'_25.mkv')
    subprocess.run(['/usr/bin/ffmpeg','-v','error','-y','-i',str(source),'-map','0:v:0','-map','0:a:0','-vf','fps=25','-c:v','libx264','-preset','fast','-crf','18','-c:a','pcm_s16le','-f','matroska',str(normalized)],check=True)
    cap=cv2.VideoCapture(str(normalized)); start=index; crops=[]; bboxes=[]; landmarks=[]
    while True:
        ok,img=cap.read()
        if not ok:break
        p=root/'full_body_img'/f'{index}.jpg';cv2.imwrite(str(p),img,[cv2.IMWRITE_JPEG_QUALITY,95])
        pts,x,y=det.detect(str(p)); pts=pts+np.array([x,y]);np.savetxt(root/'landmarks'/f'{index}.lms',pts,fmt='%d')
        bbox=compute_face_bbox(pts)
        if min(bbox)<0 or bbox[2]>img.shape[1] or bbox[3]>img.shape[0]:raise ValueError(f'invalid bbox {bbox}')
        crops.append(extract_inner(crop_face(img,bbox))); bboxes.append(bbox);landmarks.append(pts);index+=1
    cap.release();count=index-start
    raw=subprocess.check_output(['/usr/bin/ffmpeg','-v','error','-i',str(normalized),'-vn','-ac','1','-ar','16000','-f','f32le','-'])
    wave=np.frombuffer(raw,dtype='<f4').copy();wave=np.pad(wave[:count*640+80],(0,max(0,count*640+80-len(wave))))
    feat=get_feather_hubert_from_16k_speech(wave,enc,torch.device('cuda'))[:count*2].numpy().reshape(count,2,1024)
    parts.append(feat);ranges.append(dict(clip=name,start=start,end=index))
    for i in np.linspace(0,count-1,6).astype(int):thumbs.append(cv2.resize(crops[i],(216,216)))
    print(name,count,'frames',flush=True)
np.save(root/'aud_hu.npy',np.concatenate(parts));(root/'clip_ranges.json').write_text(json.dumps({'ranges':ranges},indent=2))
cv2.imwrite('/work/output/training_atlas.jpg',np.concatenate([np.concatenate(thumbs[i:i+6],axis=1) for i in range(0,len(thumbs),6)],axis=0))
# Neutral still is an independent inference resource, not silently repeated training data.
base='/work/data/base.jpg';pts,x,y=det.detect(base);np.savetxt('/work/data/base.lms',pts+np.array([x,y]),fmt='%d')
print('prepared',index,flush=True)
