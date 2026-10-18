"""Bounded PCM history for long Specter replies, using the existing LoopEngine."""
import numpy as np


class RealtimeLoopSession:
    def __init__(self, engine, mode, start_frame, teeth):
        from stream_core import MODES
        from face_utils import gather_audio_window

        if mode not in MODES or teeth != "model":
            raise ValueError("Invalid FeatherTalk mode or teeth mode")
        self.engine = engine
        self.lookahead = MODES[mode]["lookahead"]
        self.gather = gather_audio_window
        self.pcm = np.empty(0, dtype=np.float32)
        self.base_frame = self.next_frame = self.received_samples = 0
        self.teeth_restored_frames = 0
        self.last_index = self.last_image = None
        self.peak_pcm_samples = 0
        engine.begin(start_frame)

    def push(self, pcm, final=False):
        self.received_samples += len(pcm)
        self.pcm = np.concatenate([self.pcm, np.asarray(pcm, dtype=np.float32)])
        self.peak_pcm_samples = max(self.peak_pcm_samples, len(self.pcm))
        total = max(0, (self.received_samples - 80) // 640)
        if final:
            total = self.received_samples // 640
            needed = (total - self.base_frame) * 640 + 80
            self.pcm = np.pad(self.pcm, (0, max(0, needed - len(self.pcm))))
        eligible = total if final else max(0, total - self.lookahead)
        if eligible <= self.next_frame:
            return
        left = max(self.base_frame, self.next_frame - 100)
        features = self.engine.encode(self.pcm[(left - self.base_frame) * 640:])
        for index in range(self.next_frame, eligible):
            base_index = self.engine.cursor
            window = self.gather(features, index - left).numpy()
            image, _ = self.engine.render(window)
            self.engine.sync()
            self.next_frame = index + 1
            self.last_index, self.last_image = base_index, image
            yield base_index, image
        keep_from = max(self.base_frame, self.next_frame - 100)
        self.pcm = self.pcm[(keep_from - self.base_frame) * 640:]
        self.base_frame = keep_from
