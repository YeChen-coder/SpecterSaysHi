"""Independent, stateful audio-to-blendshape inference using the pinned upstream model.

This module accepts continuous 16-kHz signed PCM; packet boundaries do not reset
the Kaldi feature extractor or LSTM. It does not implement image synthesis.
"""

from __future__ import annotations

import numpy as np
import kaldi_native_fbank as knf
import torch

from talkingface.models.audio2bs_lstm import Audio2Feature
from streaming_resampler import StreamingResampler24To16


class StreamingBlendShape:
    sample_rate = 16000
    samples_per_frame = 640

    def __init__(self, checkpoint: str, device: str = "cpu") -> None:
        self.device = torch.device(device)
        self.net = Audio2Feature().to(self.device)
        state = torch.load(checkpoint, map_location=self.device, weights_only=True)
        self.net.load_state_dict(state)
        self.net.eval()
        self.reset()

    def reset(self) -> None:
        options = knf.FbankOptions()
        options.frame_opts.dither = 0
        options.frame_opts.frame_length_ms = 50
        options.frame_opts.frame_shift_ms = 20
        options.frame_opts.snip_edges = False
        options.mel_opts.num_bins = 80
        options.mel_opts.debug_mel = False
        self.fbank = knf.OnlineFbank(options)
        self.fbank.accept_waveform(self.sample_rate, [0.0] * 320)
        self.h = torch.zeros(2, 1, 192, device=self.device)
        self.c = torch.zeros(2, 1, 192, device=self.device)
        self.mel_index = 0
        self.frame_index = 0
        self.pending = bytearray()

    def push_pcm(self, pcm: bytes) -> list[tuple[int, np.ndarray]]:
        """Return (frame_index, six coefficients) for every completed 40-ms frame."""
        if len(pcm) % 2:
            raise ValueError("PCM must contain complete 16-bit samples")
        self.pending.extend(pcm)
        output = []
        frame_bytes = self.samples_per_frame * 2
        while len(self.pending) >= frame_bytes:
            block = bytes(self.pending[:frame_bytes])
            del self.pending[:frame_bytes]
            samples = np.frombuffer(block, dtype="<i2").astype(np.float32) / 32768.0
            self.fbank.accept_waveform(self.sample_rate, samples.tolist())
            mel = np.stack((self.fbank.get_frame(self.mel_index),
                            self.fbank.get_frame(self.mel_index + 1)))
            features = torch.from_numpy(mel.copy()).unsqueeze(0).float().to(self.device)
            with torch.inference_mode():
                blend, self.h, self.c = self.net(features, self.h, self.c)
            output.append((self.frame_index, blend[0, 0].cpu().numpy().copy()))
            self.mel_index += 2
            self.frame_index += 1
        return output


class RealtimeBlendShape:
    """End-to-end 24-kHz PCM -> timestamped 25-FPS six-channel control values."""

    def __init__(self, checkpoint: str, device: str = "cpu") -> None:
        self.resampler = StreamingResampler24To16()
        self.model = StreamingBlendShape(checkpoint, device)

    def reset(self) -> None:
        self.resampler.reset()
        self.model.reset()

    def push_pcm_24k(self, pcm: bytes) -> list[tuple[int, np.ndarray]]:
        pcm_16k = self.resampler.push_pcm(pcm)
        return self.model.push_pcm(pcm_16k)

    def finish(self) -> list[tuple[int, np.ndarray]]:
        return self.model.push_pcm(self.resampler.finish())
