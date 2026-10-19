"""Encode only the feathered mouth support; keep the native video visible."""
import cv2
import numpy as np

def frame_metadata(engine,index):
    source=int(engine.source_indices[index%engine.count])
    return {'type':'frame','index':index,'format':'mouth_png',
            'bbox':engine.bboxes[source].tolist(),'canvas':[720,1088]}

def encode_frame(engine,index,image):
    source=int(engine.source_indices[index%engine.count])
    x1,y1,x2,y2=map(int,engine.bboxes[source])
    patch=cv2.cvtColor(image[y1:y2,x1:x2],cv2.COLOR_BGR2BGRA)
    # RGB is already feathered. Use the hard support to avoid double blending.
    patch[:,:,3]=np.where(engine.prepared[source]['alpha'][:,:,0]>0,255,0).astype(np.uint8)
    ok,data=cv2.imencode('.png',patch,[cv2.IMWRITE_PNG_COMPRESSION,1])
    if not ok:raise RuntimeError('QinChe mouth PNG encoding failed')
    return data.tobytes()
