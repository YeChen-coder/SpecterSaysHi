"""A fixed upper dentition in head coordinates, revealed by the current lips."""
import cv2
import numpy as np
from teeth_geometry import INNER,curves,jaw_transform


class RigidUpperJaw:
    def __init__(self,donor,points,base_mesh,background,settings=None):
        settings=settings or {}
        self.settings=dict(offset=float(settings.get('offset',.075)),root=float(settings.get('root',.04)),tone=float(settings.get('tone',.85)),lip_limit=float(settings.get('lip_limit',.055)))
        if not (-.2<=self.settings['offset']<=.2 and 0<=self.settings['root']<=.15 and .5<=self.settings['tone']<=1.1 and 0<=self.settings['lip_limit']<=.12):
            raise ValueError('Rigid upper-jaw calibration is outside supported limits')
        width=float(points[308,0]-points[78,0]);h,w=donor.shape[:2]
        xx=np.arange(w,dtype=np.float32);yy=np.arange(h)[:,None]
        top,bottom=curves(points,xx);hsv=cv2.cvtColor(donor,cv2.COLOR_BGR2HSV)
        inside=np.zeros((h,w),np.uint8);cv2.fillPoly(inside,[np.rint(points[list(INNER)]).astype(np.int32)],1)
        enamel=((hsv[:,:,1]<100)&(hsv[:,:,2]>125)&(inside>0)&(yy>top)&(yy<np.minimum(top+width*.17,top+(bottom-top)*.65)))
        # The broad upper-mouth band can still contain isolated lower-tooth
        # highlights. Keep components aligned with the main upper row only.
        count,labels,components,centres=cv2.connectedComponentsWithStats(enamel.astype(np.uint8))
        if count>1:
            main=1+int(np.argmax(components[1:,cv2.CC_STAT_AREA]))
            edge=components[main,cv2.CC_STAT_TOP]+components[main,cv2.CC_STAT_HEIGHT]
            keep=[k for k in range(1,count) if components[k,cv2.CC_STAT_AREA]>=max(3,components[main,cv2.CC_STAT_AREA]*.02) and centres[k,1]<=edge]
            enamel=np.isin(labels,keep)
        if enamel.sum()<12:raise ValueError('Upper tooth reference has insufficient visible enamel')
        texture=np.empty_like(donor);texture[:]=np.asarray(background).astype(np.uint8)
        texture[enamel]=np.rint(donor[enamel].astype(np.float32)*self.settings['tone']).clip(0,255).astype(np.uint8)
        mask=enamel.astype(np.float32);extension=max(0,round(width*self.settings['root']))
        # A static tooth-root extension is built once in donor coordinates.
        # It moves with the head, not with predicted tooth brightness or lips.
        edges=[];roots=[]
        for x in range(w):
            rows=np.flatnonzero(enamel[:,x])
            if len(rows)<2:continue
            first=int(rows[0]);start=max(0,first-extension)
            colour=texture[first:min(first+2,h),x].mean(0)
            for y in range(start,first):
                shade=.75+.23*(y-start)/max(first-start,1)
                texture[y,x]=np.rint(colour*shade).clip(0,255).astype(np.uint8);mask[y,x]=1.
            if points[78,0]+width*.3<x<points[308,0]-width*.3:
                edges.append((x,float(rows[-1])));roots.append((x,float(start)))
        if not edges:raise ValueError('Upper tooth reference has no central enamel')
        self.anchor=np.asarray(np.median(edges,axis=0),np.float32)
        self.root_anchor=np.asarray(np.median(roots,axis=0),np.float32)
        # Premultiplied, softly filtered colour avoids dark double blending
        # around antialiased enamel edges. This work happens once on loading.
        self.texture=cv2.GaussianBlur(texture.astype(np.float32)*mask[...,None],(0,0),.6)
        self.mask=cv2.GaussianBlur(mask,(0,0),.6)
        self.background=np.asarray(background,np.float32)
        self.transforms=[];self.anchors=[];self.root_anchors=[];self.widths=[]
        for base in base_mesh:
            transform=jaw_transform(base,points);axis=base[263]-base[33];axis/=max(float(np.linalg.norm(axis)),1.)
            down=np.asarray([-axis[1],axis[0]],np.float32);span=float(np.linalg.norm(base[308]-base[78]))
            transform[:,2]+=down*(span*self.settings['offset'])
            self.transforms.append(transform);self.anchors.append(transform[:,:2]@self.anchor+transform[:,2]);self.widths.append(span)
            self.root_anchors.append(transform[:,:2]@self.root_anchor+transform[:,2])

    def align_lip(self,image,points,index):
        """Limit excessive upper-lip lift with a local invertible vertical map."""
        polygon=points[list(INNER)];width=self.widths[index]
        low=np.maximum(np.floor(polygon.min(0)-[2,width*.25]).astype(int),0);high=np.minimum(np.ceil(polygon.max(0)+3).astype(int),image.shape[1::-1])
        x0,y0=low;x1,y1=high;original=image[y0:y1,x0:x1].copy()
        xs=np.arange(x0,x1,dtype=np.float32);ys=np.arange(y0,y1,dtype=np.float32)[:,None];top,bottom=curves(points,xs)
        axis=self.transforms[index][:,0];slope=float(axis[1]/max(axis[0],.01))
        root=self.root_anchors[index][1]+slope*(xs-self.root_anchors[index][0])
        taper=np.clip((xs-points[78,0])/(width*.15),0,1)*np.clip((points[308,0]-xs)/(width*.15),0,1)
        delta=np.clip(root-top-.3,0,np.minimum(width*self.settings['lip_limit'],np.maximum(0,(bottom-top)*.35)))*taper
        if float(delta.max(initial=0))<.05:return points,dict(lip_adjustment_max_px=0.,lip_warp_box=[int(v) for v in [x0,y0,x1,y1]],lip_warp_changed_pixels=0),original
        above=top-width*.18;below=top+(bottom-top)*.55;peak=top+delta
        upper=above+(ys-above)*(top-above)/np.maximum(peak-above,.1)
        lower=top+(ys-peak)*(below-top)/np.maximum(below-peak,.1)
        source=np.broadcast_to(ys,(y1-y0,x1-x0)).copy()
        source=np.where((ys>=above)&(ys<=peak),upper,source)
        source=np.where((ys>peak)&(ys<below),lower,source)
        mapx=np.broadcast_to(np.arange(x1-x0,dtype=np.float32),(y1-y0,x1-x0)).copy();mapy=(source-y0).astype(np.float32)
        warped=cv2.remap(original,mapx,mapy,cv2.INTER_LINEAR,borderMode=cv2.BORDER_REPLICATE)
        selected=np.abs(source-ys)>.01;image[y0:y1,x0:x1][selected]=warped[selected]
        result=points.copy()
        for k in INNER[:11]:result[k,1]+=float(np.interp(result[k,0],xs,delta))
        return result,dict(lip_adjustment_max_px=float(delta.max()),lip_warp_box=[int(v) for v in [x0,y0,x1,y1]],lip_warp_changed_pixels=int((np.abs(warped.astype(np.int16)-original).max(2)>0).sum())),original

    def process(self,image,points,index):
        self.last_points=points.copy()
        width=float(points[308,0]-points[78,0]);gap=float(points[14,1]-points[13,1])
        row=dict(replaced=False,changed_pixels=0,outside_lips_max_difference=0,closed=gap/max(width,1.)<=.025,strength=1.)
        if row['closed'] or width<8:return row
        original_points=points.copy()
        points,lip_stats,before_all=self.align_lip(image,points,index);row.update(lip_stats);self.last_points=points
        polygon=points[list(INNER)];low=np.maximum(np.floor(polygon.min(0)-2).astype(int),0);high=np.minimum(np.ceil(polygon.max(0)+3).astype(int),image.shape[1::-1])
        x0,y0=low;x1,y1=high
        if x1<=x0 or y1<=y0:return row
        original=image[y0:y1,x0:x1].copy();xs=np.arange(x0,x1,dtype=np.float32);ys=np.arange(y0,y1,dtype=np.float32)[:,None]
        top,bottom=curves(points,xs);feather=max(.55,self.widths[index]*.008)
        inside=np.zeros(original.shape[:2],np.uint8);cv2.fillPoly(inside,[np.rint(polygon-low).astype(np.int32)],1)
        lip=(np.clip((ys-top+.15)/feather,0,1)*np.clip((bottom-.15-ys)/feather,0,1)*inside)
        transform=self.transforms[index].copy();transform[:,2]-=low
        patch=cv2.warpAffine(self.texture,transform,(x1-x0,y1-y0),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT,borderValue=0)
        tooth=cv2.warpAffine(self.mask,transform,(x1-x0,y1-y0),flags=cv2.INTER_LINEAR)
        axis=self.transforms[index][:,0];slope=float(axis[1]/max(axis[0],.01))
        fixed_bottom=self.anchors[index][1]+slope*(xs-self.anchors[index][0])
        # Remove the generated upper row only. Lower-jaw pixels below this
        # upper-cavity support remain native, unlike the old full-mouth fill.
        erase=lip*np.clip((fixed_bottom+self.widths[index]*.025-ys)/feather,0,1)*np.clip((top+(bottom-top)*.72-ys)/feather,0,1)
        # Keep the backing immediately behind the rigid row independent of the
        # generated enamel, including antialiased edges and interdental gaps.
        backing=cv2.dilate(tooth,np.ones((3,3),np.uint8))
        erase=np.maximum(erase,lip*np.clip(backing*4,0,1))
        mixed=original.astype(np.float32)*(1-erase[...,None])+self.background*erase[...,None]
        alpha=np.clip(tooth*lip,0,1)
        mixed=np.rint(mixed*(1-alpha[...,None])+patch*lip[...,None]).clip(0,255).astype(np.uint8)
        changed=np.abs(mixed.astype(np.int16)-original.astype(np.int16)).max(2)
        image[y0:y1,x0:x1]=mixed
        row.update(replaced=bool((changed>0).any()),changed_pixels=int((changed>0).sum()),outside_lips_max_difference=int(changed[inside==0].max()) if (inside==0).any() else 0,
                   mouth_box=[int(x0),int(y0),int(x1),int(y1)],fixed_upper_edge_y=float(self.anchors[index][1]))
        row['dental_outside_lips_max_difference']=row['outside_lips_max_difference']
        a,b,c,d=row['lip_warp_box'];overall=np.abs(image[b:d,a:c].astype(np.int16)-before_all.astype(np.int16)).max(2)
        support=np.zeros(before_all.shape[:2],np.uint8);cv2.fillPoly(support,[np.rint(original_points[list(INNER)]-[a,b]).astype(np.int32)],1)
        row['changed_pixels']=int((overall>0).sum());row['replaced']=row['changed_pixels']>0
        row['outside_lips_max_difference']=int(overall[support==0].max()) if (support==0).any() else 0
        return row
