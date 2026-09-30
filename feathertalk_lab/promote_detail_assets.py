"""Promote reviewed option four into compact, validated runtime assets."""
from pathlib import Path
import json,hashlib,shutil,cv2,numpy as np
source=Path('/work/output/detail_balance_20260930');target=Path('/work/data/detail_balance')
target.mkdir(exist_ok=True)
original=np.load('/work/data/loop_base/frames.npy',mmap_mode='r')
matched=np.load(source/'idle_model_frames.npy',mmap_mode='r')
boxes=np.load('/work/data/loop_base/bboxes.npy')
metadata=json.loads((source/'offline_metrics.json').read_text())
def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
assert metadata['source_base_sha256']==digest('/work/data/loop_base/idle.mp4')
assert original.shape==matched.shape and len(boxes)==len(original)
height=int(np.max(boxes[:,3]-boxes[:,1]));width=int(np.max(boxes[:,2]-boxes[:,0]))
patches=np.zeros((len(boxes),height,width,3),np.uint8)
for i,(x1,y1,x2,y2) in enumerate(boxes):
    protected=y1+int((y2-y1-1)*.55)+1
    assert np.array_equal(original[i,y1:protected,x1:x2],matched[i,y1:protected,x1:x2])
    patches[i,:y2-y1,:x2-x1]=matched[i,y1:y2,x1:x2]
np.save(target/'chin_patches.npy',patches)
shutil.copy2(source/'idle_model.mp4',target/'idle.mp4')
cv2.imwrite(str(target/'poster.jpg'),matched[0])
manifest=dict(profile='model_idle',version=1,frames=len(boxes),fps=25,
    source_base_sha256=metadata['source_base_sha256'],checkpoint_sha256=digest('/work/checkpoints/retrain_20260929/best.pth'),
    bboxes_sha256=digest('/work/data/loop_base/bboxes.npy'),patches_sha256=digest(target/'chin_patches.npy'),
    idle_video_sha256=digest(target/'idle.mp4'),filters=metadata['filters'],review='detail_balance_20260930 option 4; chin only, original idle lips preserved')
(target/'manifest.json').write_text(json.dumps(manifest,indent=2));print(json.dumps(manifest),flush=True)
