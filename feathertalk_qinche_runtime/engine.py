"""QinChe approved 2026-10-17 pipeline. No Gentleman appearance code."""
import hashlib
import json
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from stream_core import Engine
from face_utils import crop_face, extract_inner, mask_mouth, reshape_audio_feat, hwc_to_chw_tensor
from native_face_match import NativeFaceMatch

CHECKPOINT_SHA = '7c44912c27f52ff84fdc632c63446d9a835c6aa1915c1ecb7d2a0251786feace'


class QinCheEngine(Engine):
    def __init__(self):
        root = Path('/assets')
        lock = json.loads(Path('/runtime/asset_lock.json').read_text())
        for name, digest in lock.items():
            with (root/name).open('rb') as stream:
                hasher = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024*1024), b''):
                    hasher.update(chunk)
                if hasher.hexdigest() != digest:
                    raise RuntimeError(f'QinChe approved asset changed: {name}')
        self.checkpoint_path = root / 'checkpoints/epoch_100.pth'
        self.checkpoint_sha = hashlib.sha256(self.checkpoint_path.read_bytes()).hexdigest()
        if self.checkpoint_sha != CHECKPOINT_SHA:
            raise RuntimeError('QinChe checkpoint differs from the approved handoff')
        super().__init__(str(self.checkpoint_path), base_image=root/'data/base.jpg',
                         base_landmarks=root/'data/base.lms')
        torch.set_num_threads(1)
        cv2.setNumThreads(1)
        data = root/'data/dynamic_base_natural'
        self.frames = np.load(data/'frames.npy', mmap_mode='r')
        self.bboxes = np.load(data/'bboxes.npy')
        self.landmarks = np.load(data/'landmarks.npy')
        self.source_indices = np.load(data/'source_indices.npy')
        expected = np.r_[np.arange(251), np.arange(249, 0, -1)]
        if (self.frames.shape != (251, 1088, 720, 3)
                or self.landmarks.shape != (251, 110, 2)
                or self.bboxes.shape != (251, 4)
                or not np.array_equal(self.source_indices, expected)):
            raise RuntimeError('QinChe natural-listening timeline/cache is invalid')
        self.count = len(self.source_indices)
        self.references = np.asarray([extract_inner(crop_face(f, tuple(b)))
                                      for f, b in zip(self.frames, self.bboxes)])
        # Match the approved exporter exactly, including CPU float normalization.
        self.inputs = torch.stack([torch.cat([hwc_to_chw_tensor(r.copy()),
            hwc_to_chw_tensor(mask_mouth(r.copy()))],0) for r in self.references]).to(self.device)
        self.matcher = NativeFaceMatch()
        self.prepared = [self.matcher.prepare(f, p, b)
                         for f, p, b in zip(self.frames, self.landmarks, self.bboxes)]
        self.composite_last_ms = 0.
        self.composite_total_ms = 0.
        self.composite_frames = 0
        self.teeth = None
        self.begin(0)
        self.idle_video_path = root/'output/dynamic_natural/idle_loop.mp4'
        self.idle_poster_path = Path('/runtime/poster.jpg')
        if not self.idle_video_path.is_file():
            raise RuntimeError('Approved silent QinChe idle video is missing')
        start = time.perf_counter()
        with torch.inference_mode():
            self.visual(self.inputs[:1], torch.zeros(1,40,1024,device=self.device))
        for seconds in [i/10 for i in range(1,52)]+[4.12,4.14,4.52,4.54,5.12,5.14]:
            self.encode(np.zeros(round(seconds*16000),np.float32))
        self.sync()
        self.stream_shape_warmup_seconds = time.perf_counter()-start

    def begin(self, frame):
        self.cursor = int(frame) % self.count
        self.matcher.field = None

    def idle_frame(self, index):
        return self.frames[int(self.source_indices[index % self.count])].copy()

    @torch.inference_mode()
    def render(self, window):
        index = int(self.source_indices[self.cursor])
        self.cursor = (self.cursor+1) % self.count
        pred = self.visual(self.inputs[index:index+1],
            reshape_audio_feat(torch.from_numpy(window))[None].to(self.device))[0]
        prediction = (pred.cpu().numpy().transpose(1,2,0)*255).clip(0,255).astype(np.uint8)
        image = self.frames[index].copy()
        start = time.perf_counter()
        roi = self.matcher.apply(prediction, self.references[index], self.prepared[index])
        x1,y1,x2,y2 = map(int, self.bboxes[index])
        image[y1:y2,x1:x2] = roi
        self.composite_last_ms = (time.perf_counter()-start)*1000
        self.composite_total_ms += self.composite_last_ms
        self.composite_frames += 1
        return image, prediction

    def health(self):
        return dict(character='QinChe', runtime='qinche-approved-20261017',
            checkpoint=str(self.checkpoint_path), checkpoint_sha256=self.checkpoint_sha,
            base_version='natural_listening_01', base_frames=self.count, unique_base_frames=len(self.frames),
            width=720, height=1088, fps=25, compositor='NativeFaceMatch',
            teeth_mode='model', teeth_restored_frames=0, detail_profile='original',
            composite_last_ms=self.composite_last_ms,
            composite_mean_ms=self.composite_total_ms/max(1,self.composite_frames),
            stream_shape_warmup_seconds=self.stream_shape_warmup_seconds)
