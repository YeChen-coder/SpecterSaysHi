"""Restore same-character oral texture inside the audio-driven inner lip contour."""
import cv2
import numpy as np
from pathlib import Path
import pickle

INNER = (78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308,
         324, 318, 402, 317, 14, 87, 178, 88, 95)


def curves(points, xs):
    upper = points[list(INNER[:11])]
    lower = points[list((INNER[0],) + INNER[10:])]
    upper, lower = upper[np.argsort(upper[:, 0])], lower[np.argsort(lower[:, 0])]
    return np.interp(xs, upper[:, 0], upper[:, 1]), np.interp(xs, lower[:, 0], lower[:, 1])


def restore_interior(frame, points, donor, donor_points, strength=.9):
    polygon = points[list(INNER)]
    left, right = points[78, 0], points[308, 0]
    source_left, source_right = donor_points[78, 0], donor_points[308, 0]
    width, source_width = right - left, source_right - source_left
    if min(width, source_width) < 5:
        return frame
    x0, y0 = np.maximum(np.floor(polygon.min(axis=0) - 2).astype(int), 0)
    x1, y1 = np.minimum(np.ceil(polygon.max(axis=0) + 3).astype(int), frame.shape[1::-1])
    if x1 <= x0 or y1 <= y0:
        return frame
    xx, yy = np.meshgrid(np.arange(x0, x1), np.arange(y0, y1))
    source_x = source_left + (xx[0] - left) * source_width / width
    top, bottom = curves(points, xx[0])
    source_top, source_bottom = curves(donor_points, source_x)
    gap, source_gap = np.maximum(bottom - top, .1), np.maximum(source_bottom - source_top, .1)
    # Preserve tooth height; stretch the darker oral cavity below the teeth.
    source_band = np.minimum(source_gap * .65, source_width * .08)
    # Closing lips occlude the tooth band instead of squeezing every tooth into
    # a white line. This keeps the teeth's dimensions stable while speaking.
    target_band = source_band * width / source_width
    offset = yy - top
    source_y = np.where(offset <= target_band,
                        source_top + offset * source_band / np.maximum(target_band, .1),
                        source_top + source_band + (offset - target_band) *
                        (source_gap - source_band) / np.maximum(gap - target_band, .1))
    patch = cv2.remap(donor, np.broadcast_to(source_x, xx.shape).astype(np.float32),
                      source_y.astype(np.float32), cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT_101)
    mask = np.zeros(xx.shape, np.uint8)
    cv2.fillPoly(mask, [np.rint(polygon - [x0, y0]).astype(np.int32)], 255)
    alpha = cv2.GaussianBlur(mask, (3, 3), .5).astype(np.float32)[..., None] / 255 * strength
    frame[y0:y1, x0:x1] = np.clip(frame[y0:y1, x0:x1] * (1-alpha) + patch * alpha, 0, 255).astype(np.uint8)
    return frame


def jaw_transform(points, donor_points):
    """Upper dentition follows the cranium, independently of animated lip width."""
    source_axis = donor_points[263] - donor_points[33]
    target_axis = points[263] - points[33]
    denominator = float(source_axis @ source_axis)
    if denominator < 25:
        raise ValueError('donor eye landmarks are degenerate')
    a = float(source_axis @ target_axis) / denominator
    b = float(source_axis[0]*target_axis[1]-source_axis[1]*target_axis[0]) / denominator
    linear = np.array([[a, -b], [b, a]], np.float32)
    # Nose bridge is above the region animated by the talking-face renderer.
    offset = points[168] - linear @ donor_points[168]
    return np.column_stack((linear, offset)).astype(np.float32)


def tone_enamel(image, points, gain):
    """Apply fixed exposure to enamel only, keeping oral shadows and texture."""
    if not 0.4 <= gain <= 1.2:
        raise ValueError('enamel gain must be between 0.4 and 1.2')
    mask = np.zeros(image.shape[:2],np.uint8)
    cv2.fillPoly(mask,[np.rint(points[list(INNER)]).astype(np.int32)],255)
    hsv = cv2.cvtColor(image,cv2.COLOR_BGR2HSV).astype(np.float32)
    enamel = np.clip((hsv[:,:,2]-90)/70,0,1)*np.clip((95-hsv[:,:,1])/40,0,1)
    enamel *= mask/255
    multiplier = 1+(gain-1)*enamel[...,None]
    return np.clip(image.astype(np.float32)*multiplier,0,255).astype(np.uint8)


def lip_occlusion_alpha(points, x0, y0, width, height, lower_guard=.020):
    """Feather inward from float lip curves; never paint on the lower lip."""
    xs = np.arange(x0,x0+width,dtype=np.float32)
    ys = np.arange(y0,y0+height,dtype=np.float32)[:,None]
    top,bottom = curves(points,xs)
    left,right = points[78,0],points[308,0]
    mouth_width = max(float(right-left),1.)
    feather = max(.5,mouth_width*.010)
    upper_inset = mouth_width*.004
    lower_inset = mouth_width*lower_guard
    upper = np.clip((ys-top-upper_inset)/feather,0,1)
    lower = np.clip((bottom-lower_inset-ys)/feather,0,1)
    sides = np.clip((xs-left)/feather,0,1)*np.clip((right-xs)/feather,0,1)
    return (upper*lower*sides)[...,None].astype(np.float32)


def restore_fixed_jaw(frame, points, donor, transform, background, strength=1.,
                      occlusion='strict_lips', lower_guard=.020):
    polygon = points[list(INNER)]
    x0, y0 = np.maximum(np.floor(polygon.min(axis=0)-2).astype(int), 0)
    x1, y1 = np.minimum(np.ceil(polygon.max(axis=0)+3).astype(int), frame.shape[1::-1])
    if x1 <= x0 or y1 <= y0:
        return frame
    matrix = transform.copy()
    matrix[:, 2] -= (x0, y0)
    # Sample the donor in a rigid head coordinate system. The mouth contour
    # below is only an occlusion mask; it never changes the texture coordinates.
    patch = cv2.warpAffine(donor, matrix, (x1-x0, y1-y0), flags=cv2.INTER_CUBIC,
                           borderMode=cv2.BORDER_CONSTANT, borderValue=background)
    if occlusion == 'strict_lips':
        # Replace the oral interior up to its subpixel boundary. A separate
        # inward lower-lip shadow hides enamel and removes the old model's
        # bright fringe without touching pixels on the lip itself.
        alpha = lip_occlusion_alpha(points,x0,y0,x1-x0,y1-y0,0.)*strength
        xs = np.arange(x0,x1,dtype=np.float32)
        ys = np.arange(y0,y1,dtype=np.float32)[:,None]
        _,bottom = curves(points,xs)
        mouth_width = max(float(points[308,0]-points[78,0]),1.)
        shadow = np.clip((bottom-mouth_width*lower_guard-ys)/max(.5,mouth_width*.010),0,1)[...,None]
        patch = np.asarray(background,np.float32)+(patch.astype(np.float32)-background)*shadow
    else:
        mask = np.zeros((y1-y0, x1-x0), np.uint8)
        cv2.fillPoly(mask, [np.rint(polygon-[x0,y0]).astype(np.int32)], 255)
        alpha = cv2.GaussianBlur(mask, (3, 3), .5).astype(np.float32)[..., None] / 255 * strength
    frame[y0:y1,x0:x1] = np.clip(frame[y0:y1,x0:x1]*(1-alpha)+patch*alpha,0,255).astype(np.uint8)
    return frame


class TeethRestorer:
    def __init__(self, avatar_directory, config, mode='source', strength=.85):
        if mode not in ('model', 'source') or not 0 <= strength <= 1:
            raise ValueError('teeth mode must be model/source, strength must be between 0 and 1')
        self.mode, self.strength = mode, strength
        self.enabled = False
        self.frames = self.replaced = self.misses = 0
        self.visibility = 0.
        self.mapping = config.get('teeth_mapping', 'lip_scaled')
        if self.mapping not in ('lip_scaled', 'fixed_jaw'):
            raise ValueError('teeth_mapping must be lip_scaled or fixed_jaw')
        self.transform = None
        self.scale_samples = []
        self.occlusion = config.get('teeth_occlusion','feathered_polygon')
        self.enamel_gain = float(config.get('teeth_enamel_gain',1.))
        self.lower_guard = float(config.get('teeth_lower_lip_guard',.020))
        if self.occlusion not in ('feathered_polygon','strict_lips') or not 0 <= self.lower_guard <= .08:
            raise ValueError('invalid tooth occlusion configuration')
        filename = config.get('teeth_file')
        if mode == 'model' or not filename or strength == 0:
            return
        if Path(filename).name != filename:
            raise ValueError('teeth_file must be a filename in the avatar directory')
        with np.load(Path(avatar_directory) / filename, allow_pickle=False) as archive:
            self.donor = archive['image']
            self.points = archive['landmarks']
        if (self.donor.dtype != np.uint8 or self.donor.ndim != 3 or self.donor.shape[2] != 3
                or self.points.shape != (478, 2) or not np.isfinite(self.points).all()):
            raise ValueError('teeth asset must contain a BGR image and 478 finite XY landmarks')
        if self.enamel_gain != 1.:
            self.donor = tone_enamel(self.donor,self.points,self.enamel_gain)
        import mediapipe as mp
        self.mesh = mp.solutions.face_mesh.FaceMesh(max_num_faces=1, refine_landmarks=True,
                                                   min_detection_confidence=.5,
                                                   min_tracking_confidence=.5)
        if self.mapping == 'fixed_jaw':
            self._prepare_head_trajectory(avatar_directory)
        self.enabled = True

    def _prepare_head_trajectory(self, avatar_directory):
        from scipy.ndimage import gaussian_filter1d
        capture = cv2.VideoCapture(str(Path(avatar_directory)/'circle.mp4'))
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        ok, first = capture.read()
        capture.release()
        if not ok:
            raise ValueError('cannot read reference video to anchor fixed dentition')
        height, width = first.shape[:2]
        scale = min(1.,640/max(height,width))
        tracked = self.mesh.process(cv2.cvtColor(cv2.resize(first,(round(width*scale),round(height*scale))),cv2.COLOR_BGR2RGB))
        if not tracked.multi_face_landmarks:
            raise ValueError('cannot find the reference head for fixed dentition')
        anchor_points = np.asarray([(p.x*width,p.y*height) for p in tracked.multi_face_landmarks[0].landmark],np.float32)
        # This trusted local PKL is the same landmark asset loaded by RenderModel.
        with (Path(avatar_directory)/'keypoint_rotate.pkl').open('rb') as stream:
            geometry = np.asarray(pickle.load(stream))
        if geometry.shape != (count,478,3) or not np.isfinite(geometry).all():
            raise ValueError('reference head trajectory must match video frames')
        geometry = gaussian_filter1d(geometry[:,:,:2],sigma=1.25,axis=0,mode='wrap')
        anchor = jaw_transform(anchor_points,self.points)
        transforms = []
        for points in geometry:
            delta = jaw_transform(points,geometry[0])
            linear = delta[:,:2] @ anchor[:,:2]
            offset = delta[:,:2] @ anchor[:,2] + delta[:,2]
            transforms.append(np.column_stack((linear,offset)))
        self.head_transforms = np.asarray(transforms,np.float32)
        # Do not import the donor's lip texture into a different mouth aperture.
        mask = np.zeros(self.donor.shape[:2],np.uint8)
        cv2.fillPoly(mask,[np.rint(self.points[list(INNER)]).astype(np.int32)],255)
        pixels = self.donor[mask>0]
        brightness = pixels.mean(axis=1)
        dark = pixels[brightness <= np.percentile(brightness,25)].mean(axis=0)
        self.background = tuple(float(value) for value in dark)
        alpha = cv2.GaussianBlur(mask,(3,3),.5).astype(np.float32)[...,None]/255
        self.donor = np.clip(self.donor*alpha+dark*(1-alpha),0,255).astype(np.uint8)

    def reset(self):
        self.visibility = 0.
        self.transform = None
        # Discard tracking state when playback seeks to a different frame.
        if self.enabled:
            self.mesh.reset()

    def process(self, frame, frame_index=None):
        if not self.enabled:
            return frame
        self.frames += 1
        height, width = frame.shape[:2]
        scale = min(1., 640 / max(height, width))
        small = cv2.resize(frame, (round(width * scale), round(height * scale)))
        result = self.mesh.process(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
        if not result.multi_face_landmarks:
            self.misses += 1
            self.visibility = 0.
            return frame
        points = np.asarray([(p.x * width, p.y * height) for p in
                             result.multi_face_landmarks[0].landmark], np.float32)
        if self.mapping == 'fixed_jaw':
            if frame_index is None:
                raise ValueError('fixed dentition requires the reference video frame index')
            transform = self.head_transforms[frame_index % len(self.head_transforms)]
            scale = float(np.linalg.norm(transform[:,0]))
            self.scale_samples.append(scale)
            if len(self.scale_samples) > 2000:
                del self.scale_samples[:1000]
            self.replaced += 1
            return restore_fixed_jaw(frame, points, self.donor, transform,
                                     self.background, self.strength,self.occlusion,self.lower_guard)
        polygon = points[list(INNER)]
        x0, y0 = np.maximum(np.floor(polygon.min(axis=0)-1).astype(int), 0)
        x1, y1 = np.minimum(np.ceil(polygon.max(axis=0)+2).astype(int), (width, height))
        if x1 <= x0 or y1 <= y0:
            return frame
        mask = np.zeros((y1-y0, x1-x0), np.uint8)
        cv2.fillPoly(mask, [np.rint(polygon-[x0,y0]).astype(np.int32)], 255)
        hsv = cv2.cvtColor(frame[y0:y1,x0:x1], cv2.COLOR_BGR2HSV)
        tooth_fraction = np.count_nonzero((hsv[...,1] < 75) & (hsv[...,2] > 130) & (mask > 0)) / max(np.count_nonzero(mask), 1)
        gap = max(float(points[14,1]-points[13,1]), 0)
        mouth_width = max(float(np.linalg.norm(points[291]-points[61])), 1)
        opening = np.clip((gap/mouth_width-.025)/.04, 0, 1)
        visible = np.clip((tooth_fraction-.04)/.16, 0, 1) * opening
        # Smooth only texture visibility; mouth geometry and audio stay current.
        self.visibility = .7 * visible + .3 * self.visibility
        strength = self.strength * min(self.visibility, opening)
        if strength > .01:
            self.replaced += 1
            return restore_interior(frame, points, self.donor, self.points, strength)
        return frame

    def status(self):
        return {'mode': self.mode, 'enabled': self.enabled, 'strength': self.strength,
                'mapping': self.mapping,
                'occlusion': self.occlusion, 'enamel_gain': self.enamel_gain,
                'lower_lip_guard': self.lower_guard,
                'geometry_source': 'reference_video' if self.mapping == 'fixed_jaw' else 'generated_lips',
                'head_scale_p5_p95': np.percentile(self.scale_samples,[5,95]).round(5).tolist() if self.scale_samples else None,
                'tracked_frames': self.frames, 'restored_frames': self.replaced,
                'tracking_misses': self.misses}
