"""Standalone invariant and alias rejection checks for the 24-to-16 kHz resampler."""

import sys
from pathlib import Path

import numpy as np

# Support the exported checkout as well as the original container mount.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dh_live_full"))
sys.path.insert(0, "/app")
from streaming_resampler import StreamingResampler24To16


samples = (np.sin(2 * np.pi * 1700 * np.arange(24000) / 24000) * 15000).astype("<i2")
pcm = samples.tobytes()


def convert(packet_bytes):
    resampler = StreamingResampler24To16()
    packets = [resampler.push_pcm(pcm[i:i + packet_bytes])
               for i in range(0, len(pcm), packet_bytes)]
    return b"".join(packets) + resampler.finish()


whole = convert(len(pcm))
assert len(whole) == 16000 * 2
assert whole == convert(4800)  # 100-ms network packets
assert whole == convert(9600)  # 200-ms network packets
assert whole == convert(274)  # Arbitrary even-sized packets


def rms_at(frequency):
    tone = (np.sin(2 * np.pi * frequency * np.arange(24000) / 24000) * 15000).astype("<i2")
    out = np.frombuffer(StreamingResampler24To16().push_pcm(tone.tobytes()), dtype="<i2")[200:]
    return float(np.sqrt(np.mean(out.astype(np.float64) ** 2)))


passband = rms_at(3000)
stopband = rms_at(10000)
assert stopband < passband * 0.02, (passband, stopband)
print({"output_samples": len(whole) // 2, "packet_invariant": True,
       "10kHz_to_3kHz_rms_ratio": stopband / passband})
