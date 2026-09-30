"""Independent 184x184 mouth compositor using the user's DINet mini checkpoint.

Inputs match the two RGBA buffers passed to the upstream WASM processImage:
source video crop and WebGL morphology result. Browser-side geometry generation is
still a separate stage.
"""

from __future__ import annotations

import json
import gzip

import numpy as np
import torch

from talkingface.models.DINet_mini import DINet_mini_pipeline2, model_size
from mouth_compositor import compose_mouth


class IndependentImageRenderer:
    def __init__(self, checkpoint: str, avatar_data: str, device: str = "cpu") -> None:
        self.device = torch.device(device)
        self.model = DINet_mini_pipeline2(3, 12, cuda=self.device.type == "cuda").to(self.device)
        state = torch.load(checkpoint, map_location=self.device, weights_only=True)
        self.model.infer_model.load_state_dict(state["state_dict"]["net_g"])
        self.model.eval()
        with gzip.open(avatar_data, "rt", encoding="utf-8") as file:
            metadata = json.load(file)
        if metadata["size"] != model_size or len(metadata["ref_data"]) < 64:
            raise ValueError("avatar package does not match the 184-pixel model")
        self.model.infer_model.ref_in_feature = torch.tensor(
            metadata["ref_data"][:64], dtype=torch.float32, device=self.device
        ).reshape(1, 64)

    def render(self, source_rgba: bytes, geometry_rgba: bytes) -> bytes:
        expected = model_size * model_size * 4
        if len(source_rgba) != expected or len(geometry_rgba) != expected:
            raise ValueError(f"both RGBA inputs must contain {expected} bytes")

        def as_tensor(raw: bytes) -> torch.Tensor:
            image = np.frombuffer(raw, dtype=np.uint8).reshape(model_size, model_size, 4)
            return torch.from_numpy(image.copy()).permute(2, 0, 1).unsqueeze(0).to(
                self.device, dtype=torch.float32
            ) / 255.0

        with torch.inference_mode():
            output = self.model.interface(as_tensor(source_rgba), as_tensor(geometry_rgba))
        rgba = (output[0].permute(1, 2, 0).clamp(0, 1) * 255).round().byte().cpu().numpy()
        return compose_mouth(source_rgba, rgba.tobytes())
