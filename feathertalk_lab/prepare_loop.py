"""Prepare an isolated copy of the actual DH idle video for moving-base inference."""
import os,sys,json,hashlib,time
from pathlib import Path
import cv2,numpy as np,torch
from face_utils import compute_face_bbox

torch.set_num_threads(4)
torch.cuda.set_per_process_memory_fraction(.25)
sys.path.insert(0,'/opt/FeatherTalk/data_utils')
os.chdir('/opt/FeatherTalk/data_utils')
from get_landmark import Landmark,face_det
det=Landmark()
root=Path('/work/data/loop_base');out=Path('/work/output/loop_base')
out.mkdir(exist_ok=True)
source=root/'idle.mp4';cap=cv2.VideoCapture(str(source))
fps=cap.get(cv2.CAP_PROP_FPS);count=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH));height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
if abs(fps-25)>.01:raise ValueError('This adapter requires a 25 fps source')
frames=np.lib.format.open_memmap(root/'frames.npy',mode='w+',dtype=np.uint8,shape=(count,height,width,3))
points=[];begin=time.perf_counter()
with torch.inference_mode():
    for i in range(count):
        ok,img=cap.read()
        if not ok:raise ValueError(f'Decode failed at {i}')
        frames[i]=img
        faces,boxes,_,_=face_det(img,det.det_net)
        if not faces:raise ValueError(f'No face at {i}')
        face=faces[0];h,w=face.shape[:2]
        x=torch.from_numpy((cv2.resize(face,(192,192)).astype(np.float32)/255).transpose(2,0,1))[None].to(det.device)
        pts=(det.pfld_backbone(x)[0].cpu().numpy()+det.mean_face).reshape(-1,2)
        pts[:,0]*=w;pts[:,1]*=h
        points.append(pts+np.asarray(boxes[0][:2]))
        if i%50==0:print('PREPARE_LOOP',i,count,flush=True)
cap.release();frames.flush()
raw=np.asarray(points,np.float32)
# Offline cyclic smoothing removes detector jitter without locking the head pose.
smooth=sum(np.roll(raw,k,axis=0)*weight for k,weight in [(-2,1),(-1,2),(0,3),(1,2),(2,1)])/9
landmarks=np.rint(smooth).astype(np.int32)
bboxes=np.asarray([compute_face_bbox(pts) for pts in landmarks],np.int32)
if np.any(bboxes[:,:2]<0) or np.any(bboxes[:,2]>width) or np.any(bboxes[:,3]>height):raise ValueError('Invalid face bounds')
np.save(root/'landmarks.npy',landmarks);np.save(root/'bboxes.npy',bboxes)
cv2.imwrite(str(root/'poster.jpg'),frames[0])
tiles=[]
for i in np.linspace(0,count-1,8).astype(int):
    x1,y1,x2,y2=bboxes[i];tile=cv2.resize(frames[i,y1:y2,x1:x2],(200,200))
    cv2.putText(tile,str(i),(6,20),cv2.FONT_HERSHEY_SIMPLEX,.6,(0,255,255),1);tiles.append(tile)
cv2.imwrite(str(out/'base_pose_atlas.jpg'),np.concatenate(tiles,axis=1))
report={'source':'isolated copy of dh_live_full/avatar/idle_rendered.mp4',
    'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'frames':count,'fps':fps,
    'width':width,'height':height,'duration_seconds':count/fps,'prepare_seconds':time.perf_counter()-begin,
    'bbox_min':bboxes.min(axis=0).tolist(),'bbox_max':bboxes.max(axis=0).tolist(),
    'landmark_smoothing':'cyclic 5 frames, weights 1 2 3 2 1; offline only; no audio lookahead added'}
(root/'manifest.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
