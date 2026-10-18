"""Keep a close-portrait avatar's generated lower face away from its collar."""
import cv2
import numpy as np

def lower_face_support(points,bbox):
    x1,y1,x2,y2=map(int,bbox)
    # WFLW jaw points 0..32 run left-to-right around the chin. Expand slightly
    # for jaw articulation, then feather into native skin; clothes stay native.
    jaw=np.asarray(points[:33],np.float32).copy()
    jaw[:,0]=(jaw[:,0]-x1)*152/(x2-x1);jaw[:,1]=(jaw[:,1]-y1)*152/(y2-y1)
    polygon=np.concatenate([jaw,[[152,0],[0,0]]])
    mask=np.zeros((152,152),np.uint8)
    cv2.fillPoly(mask,[np.rint(polygon).astype(np.int32)],255)
    mask=cv2.dilate(mask,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(9,9)))
    return cv2.GaussianBlur(mask.astype(np.float32)/255,(9,9),2)
