"""A reproducible rest pose shared by cached idle and the live speech tail."""
import numpy as np


def rest_mouth(audio_model, frames=100):
    audio_model.reset()
    silent = np.zeros(640, np.float32)
    for _ in range(frames):
        mouth = audio_model.interface_frame(silent)
    return mouth


def settle_mouth(last, rest, progress):
    # Smoothstep starts and finishes with zero slope. Texture coordinates and
    # the original head trajectory stay unchanged throughout the transition.
    t = float(np.clip(progress, 0, 1))
    eased = t*t*(3-2*t)
    return np.rint(last.astype(np.float32)*(1-eased)+rest*eased).astype(np.uint8)
