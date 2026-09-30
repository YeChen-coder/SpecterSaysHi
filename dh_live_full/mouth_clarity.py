"""Bounded luma sharpening shared by live speech, settling and cached idle."""
import pickle
import time
from pathlib import Path

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter1d


class MouthClarity:
    def __init__(self, avatar_directory, config):
        self.strength = float(config.get('mouth_clarity_strength', 0.))
        if not 0 <= self.strength <= 1:
            raise ValueError('mouth_clarity_strength must be between 0 and 1')
        self.enabled = self.strength > 0
        self.frames = 0
        self.total_ms = self.last_ms = 0.
        self.regions = []
        if not self.enabled:
            return
        # Same trusted local landmark asset used by RenderModel. Anchor the
        # region to the source head, independently of animated mouth width.
        with (Path(avatar_directory)/'keypoint_rotate.pkl').open('rb') as stream:
            geometry = np.asarray(pickle.load(stream))
        if geometry.ndim != 3 or geometry.shape[1:] != (478, 3) or not len(geometry) or not np.isfinite(geometry).all():
            raise ValueError('mouth clarity requires finite 478-point source geometry')
        geometry = gaussian_filter1d(geometry[:, :, :2], 1.25, axis=0, mode='wrap')
        for points in geometry:
            width = max(float(np.linalg.norm(points[291]-points[61])), 20.)
            center = (points[13]+points[14])*.5
            rx, ry = width*1.15, width*.65
            x0, y0 = np.floor(center-[rx, ry]).astype(int)
            x1, y1 = np.ceil(center+[rx, ry]).astype(int)
            xx, yy = np.meshgrid(np.arange(x0, x1), np.arange(y0, y1))
            distance = ((xx-center[0])/rx)**2+((yy-center[1])/ry)**2
            mask = np.clip((1-distance)/.5, 0, 1).astype(np.float32)
            self.regions.append((x0, y0, x1, y1, mask))

    def process(self, image, index):
        if not self.enabled:
            return image
        started = time.perf_counter()
        x0, y0, x1, y1, mask = self.regions[index % len(self.regions)]
        # Clip only the spatial region if a different avatar touches an edge.
        left, top = max(0, x0), max(0, y0)
        right, bottom = min(image.shape[1], x1), min(image.shape[0], y1)
        if right <= left or bottom <= top:
            return image
        mask = mask[top-y0:bottom-y0, left-x0:right-x0]
        roi = image[top:bottom, left:right]
        colors = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
        luma = colors[:, :, 0].astype(np.float32)
        blur = cv2.GaussianBlur(luma, (5, 5), .8)
        detail = luma-blur
        detail = np.sign(detail)*np.maximum(np.abs(detail)-1., 0.)
        delta = np.clip(detail*self.strength, -4., 4.)*mask
        colors[:, :, 0] = np.rint(np.clip(luma+delta, 0, 255)).astype(np.uint8)
        enhanced = cv2.cvtColor(colors, cv2.COLOR_YCrCb2BGR)
        # Preserve exact pixels wherever no correction was applied: a color
        # space round trip alone must not alter the rest of the image.
        selected = np.abs(delta) >= .5
        roi[selected] = enhanced[selected]
        self.frames += 1
        self.last_ms = (time.perf_counter()-started)*1000
        self.total_ms += self.last_ms
        return image

    def status(self):
        return {'enabled': self.enabled, 'strength': self.strength,
                'frames': self.frames, 'last_ms': round(self.last_ms, 3),
                'mean_ms': round(self.total_ms/max(self.frames, 1), 3)}
