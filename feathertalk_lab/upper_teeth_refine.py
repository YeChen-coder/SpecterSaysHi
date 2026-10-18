"""Refine existing upper enamel without painting the surrounding oral cavity."""
import cv2
import numpy as np
from teeth_geometry import INNER, curves


def refine_upper_enamel(image, points, donor, donor_points, transform, strength, *, state=None):
    polygon = points[list(INNER)]
    low = np.maximum(np.floor(polygon.min(0)-2).astype(int), 0)
    high = np.minimum(np.ceil(polygon.max(0)+3).astype(int), image.shape[1::-1])
    x0,y0 = low; x1,y1 = high
    if x1 <= x0 or y1 <= y0:
        return dict(changed_pixels=0, replaced=False, outside_lips_max_difference=0)
    original = image[y0:y1,x0:x1].copy()
    xs = np.arange(x0,x1,dtype=np.float32)
    ys = np.arange(y0,y1,dtype=np.float32)[:,None]
    top,bottom = curves(points,xs)
    width = float(points[308,0]-points[78,0])
    inside = np.zeros(original.shape[:2],np.uint8)
    cv2.fillPoly(inside,[np.rint(polygon-low).astype(np.int32)],1)
    hsv = cv2.cvtColor(original,cv2.COLOR_BGR2HSV)
    # Restrict detection to the upper row. Existing lower teeth and dark oral
    # pixels are never replacement targets, even when the mouth is wide open.
    upper = (ys > top) & (ys < top+(bottom-top)*.60)
    target = (hsv[:,:,1]<100) & (hsv[:,:,2]>125) & upper & (inside>0)
    central = (xs > points[78,0]+width*.25) & (xs < points[308,0]-width*.25)
    anchor = target & central
    if anchor.sum()<4:
        return dict(changed_pixels=0, replaced=False, outside_lips_max_difference=0)
    # Use the generated upper-enamel location as a residual alignment cue;
    # preserve head-based tooth dimensions instead of stretching to the lips.
    target_y = float(np.mean(np.nonzero(anchor)[0])+y0)
    donor_hsv = cv2.cvtColor(donor,cv2.COLOR_BGR2HSV)
    donor_xs = np.arange(donor.shape[1],dtype=np.float32)
    donor_top,_ = curves(donor_points,donor_xs)
    donor_width = float(donor_points[308,0]-donor_points[78,0])
    donor_ys = np.arange(donor.shape[0])[:,None]
    enamel = ((donor_hsv[:,:,1]<100)&(donor_hsv[:,:,2]>125)&
              (donor_ys>donor_top)&(donor_ys<donor_top+donor_width*.115))
    donor_central = ((donor_xs>donor_points[78,0]+donor_width*.25)&
                     (donor_xs<donor_points[308,0]-donor_width*.25))
    donor_anchor = enamel & donor_central
    if donor_anchor.sum()<4:
        return dict(changed_pixels=0, replaced=False, outside_lips_max_difference=0)
    dy,dx = np.nonzero(donor_anchor)
    head_y = float((transform[:,:2] @ [float(dx.mean()),float(dy.mean())]+transform[:,2])[1])
    shift = float(np.clip(target_y-head_y,-width*.18,width*.18))
    stable=state is not None
    if stable:
        measured=shift/max(width,1.)
        previous=state.get('alignment',measured)
        normalized=previous+float(np.clip((measured-previous)*.22,-.012,.012))
        state['alignment']=normalized
        shift=normalized*width
    aligned = transform.copy();aligned[:,2]-=low;aligned[1,2]+=shift
    patch = cv2.warpAffine(donor,aligned,(x1-x0,y1-y0),flags=cv2.INTER_CUBIC)
    donor_mask = cv2.warpAffine(enamel.astype(np.float32),aligned,(x1-x0,y1-y0),flags=cv2.INTER_LINEAR)
    donor_mask = cv2.GaussianBlur(donor_mask,(0,0),.55)
    target_mask = cv2.GaussianBlur(target.astype(np.float32),(0,0),.55)
    # Hard support prevents even feathering from erasing gums, mouth shadows
    # or lower teeth. The overlay can refine an existing row, never invent one.
    alpha = np.clip(donor_mask*target_mask*strength,0,1)*target
    if stable:
        # Threshold confidence approaches zero continuously. A donor supplies
        # bounded fine detail rather than replacing tooth colour/brightness.
        confidence=np.clip((hsv[:,:,2].astype(np.float32)-125)/35,0,1)*np.clip((100-hsv[:,:,1].astype(np.float32))/25,0,1)
        alpha*=confidence
        donor_luma=cv2.cvtColor(patch,cv2.COLOR_BGR2YCrCb)[:,:,0].astype(np.float32)
        detail=np.clip(donor_luma-cv2.GaussianBlur(donor_luma,(0,0),1.0),-8,8)
        colour=cv2.cvtColor(original,cv2.COLOR_BGR2YCrCb)
        colour[:,:,0]=np.rint(colour[:,:,0].astype(np.float32)+detail*alpha).clip(0,255).astype(np.uint8)
        candidate=cv2.cvtColor(colour,cv2.COLOR_YCrCb2BGR)
        # Also bound RGB conversion rounding and leave unselected pixels exact.
        correction=np.clip(candidate.astype(np.int16)-original.astype(np.int16),-6,6)
        mixed=(original.astype(np.int16)+correction).clip(0,255).astype(np.uint8)
    else:
        mixed = np.rint(original*(1-alpha[...,None])+patch*alpha[...,None]).clip(0,255).astype(np.uint8)
    selected = alpha>0
    mixed[~selected]=original[~selected]
    image[y0:y1,x0:x1][selected] = mixed[selected]
    diff = np.abs(mixed.astype(np.int16)-original.astype(np.int16))
    changed = int((diff.max(2)>0).sum())
    return dict(changed_pixels=changed,replaced=changed>0,outside_lips_max_difference=0,
                mouth_box=[int(x0),int(y0),int(x1),int(y1)],enamel_alignment_y_px=shift,
                max_enamel_correction=int(diff[selected].max()) if selected.any() else 0)
