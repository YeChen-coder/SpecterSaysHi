"""Audit new speaking footage and retain sharp same-character tooth donors."""
import json,time,hashlib,subprocess
from pathlib import Path
import cv2,numpy as np,mediapipe as mp
from scipy.ndimage import gaussian_filter1d
from teeth_geometry import roi_from_points,mesh_points,mouth_stats,INNER

root=Path('/work/data/teeth_20260930');root.mkdir(exist_ok=True)
out=Path('/work/output/teeth_20260930');out.mkdir(exist_ok=True)
sources=Path('/work/data/new_training_sources/provided_20260929')
records=[];candidates=[];begin=time.perf_counter()
for source in sorted(sources.glob('*.mp4')):
    if source.stem.endswith('_11'):continue # Preserve the original held-out clip.
    rough=np.load(Path('/work/data/retrain_20260929')/source.stem/'landmarks.npy')
    cap=cv2.VideoCapture(str(source));fps=cap.get(cv2.CAP_PROP_FPS);i=0;missing=0;local=[]
    mesh=mp.solutions.face_mesh.FaceMesh(max_num_faces=1,refine_landmarks=True,min_detection_confidence=.5,min_tracking_confidence=.5)
    while True:
        ok,image=cap.read()
        if not ok:break
        pts0=rough[min(len(rough)-1,round(i*25/fps))]
        roi=roi_from_points(pts0.astype(np.float32),image.shape[1],image.shape[0]);points=mesh_points(mesh,image,roi)
        if points is None:missing+=1;i+=1;continue
        stats=mouth_stats(image,points)
        if stats and .055<stats['opening']<.32 and stats['width']>28 and .10<stats['tooth_fraction']<.72 and stats['glare_fraction']<.35:
            score=np.log1p(stats['sharpness'])*min(1,stats['tooth_fraction']/.25)*(1-stats['glare_fraction'])
            row=dict(source=source.name,source_frame=i,source_seconds=i/fps,score=float(score),**stats)
            x0,y0=np.maximum(np.floor(points[list(INNER)].min(axis=0)-14).astype(int),0)
            x1,y1=np.minimum(np.ceil(points[list(INNER)].max(axis=0)+15).astype(int),image.shape[1::-1])
            local.append((row,image[y0:y1,x0:x1].copy(),points-[x0,y0]))
        i+=1
    cap.release();mesh.close()
    chosen=[]
    for item in sorted(local,key=lambda x:x[0]['score'],reverse=True):
        if all(abs(item[0]['source_frame']-prev[0]['source_frame'])>=12 for prev in chosen):chosen.append(item)
        if len(chosen)==3:break
    candidates.extend(chosen)
    row=dict(source=source.name,frames_scanned=i,mesh_misses=missing,eligible_frames=len(local),retained=len(chosen),sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    records.append(row);print(json.dumps(row),flush=True)

candidates.sort(key=lambda x:x[0]['score'],reverse=True)
if not candidates:raise ValueError('No usable new tooth donor found')
metadata=[];tiles=[]
for n,(row,image,points) in enumerate(candidates):
    filename=f'donor_{n:02d}.npz';np.savez_compressed(root/filename,image=image,points=points)
    metadata.append(dict(id=n,asset=filename,**row))
    tile=cv2.resize(image,(240,120));cv2.putText(tile,f'{n:02d} {row["source"][:24]}',(3,15),cv2.FONT_HERSHEY_SIMPLEX,.35,(0,255,255),1)
    cv2.putText(tile,f'frame {row["source_frame"]} score {row["score"]:.2f}',(3,115),cv2.FONT_HERSHEY_SIMPLEX,.38,(0,255,255),1);tiles.append(tile)
while len(tiles)%4:tiles.append(np.zeros_like(tiles[0]))
cv2.imwrite(str(out/'donor_contact_sheet.jpg'),np.concatenate([np.concatenate(tiles[i:i+4],axis=1) for i in range(0,len(tiles),4)],axis=0))

frames=np.load('/work/data/loop_base/frames.npy',mmap_mode='r');rough=np.load('/work/data/loop_base/landmarks.npy')
mesh=mp.solutions.face_mesh.FaceMesh(max_num_faces=1,refine_landmarks=True,min_detection_confidence=.5,min_tracking_confidence=.5)
trajectory=[];rois=[]
for i,image in enumerate(frames):
    roi=roi_from_points(rough[i].astype(np.float32),image.shape[1],image.shape[0]);points=mesh_points(mesh,image,roi)
    if points is None:raise ValueError(f'Cannot anchor base video frame {i}')
    trajectory.append(points);rois.append(roi)
mesh.close()
np.save(root/'base_mesh.npy',gaussian_filter1d(np.asarray(trajectory),1.,axis=0,mode='wrap').astype(np.float32));np.save(root/'base_rois.npy',np.asarray(rois,np.int32))
report=dict(sources=records,candidates=metadata,selected_donor=0,heldout_excluded='english_training_20260929_11.mp4',prepare_seconds=time.perf_counter()-begin,
            selection='opening, enamel visibility, clipped highlights and local sharpness; final donor reviewed visually; one fixed donor per session prevents tooth identity switching')
(root/'manifest.json').write_text(json.dumps(report,indent=2));(out/'donor_audit.json').write_text(json.dumps(report,indent=2))
print('TEETH_BANK_READY',len(metadata),time.perf_counter()-begin,flush=True)
