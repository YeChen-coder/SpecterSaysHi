"""Blend generated articulation into the source crop without replacing the jaw."""

from __future__ import annotations

import numpy as np

SIZE = 184


def _mouth_mask() -> np.ndarray:
    y, x = np.mgrid[:SIZE, :SIZE].astype(np.float32)
    radius = np.sqrt(((x - 92.0) / 64.0) ** 2 + ((y - 94.0) / 36.0) ** 2)
    # Full influence around the lips, feathering to zero before the nose,
    # cheeks, and chin. The source video remains the color/shape anchor.
    return (0.88 * np.clip((1.06 - radius) / 0.36, 0.0, 1.0))[..., None]


MOUTH_MASK = _mouth_mask()


def compose_mouth(source_rgba: bytes, generated_rgba: bytes) -> bytes:
    expected = SIZE * SIZE * 4
    if len(source_rgba) != expected or len(generated_rgba) != expected:
        raise ValueError(f"both RGBA images must contain {expected} bytes")

    source = np.frombuffer(source_rgba, dtype=np.uint8).reshape(SIZE, SIZE, 4)
    generated = np.frombuffer(generated_rgba, dtype=np.uint8).reshape(SIZE, SIZE, 4)
    original_rgb = source[..., :3].astype(np.float32)
    difference = generated[..., :3].astype(np.float32) - original_rgb
    luminance = (difference[..., 0] * 0.299 +
                 difference[..., 1] * 0.587 +
                 difference[..., 2] * 0.114)[..., None]
    # Preserve the generated light/dark mouth opening but temper the model's
    # lip hue and saturation shifts against this exact source frame.
    corrected = original_rgb + luminance + 0.55 * (difference - luminance)
    result = source.copy()
    result[..., :3] = np.clip(
        original_rgb * (1.0 - MOUTH_MASK) + corrected * MOUTH_MASK,
        0, 255
    ).round().astype(np.uint8)
    return result.tobytes()
