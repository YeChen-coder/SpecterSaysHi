"""Use curated appearance frames independently of the natural idle video."""
from pathlib import Path

import numpy as np
import torch


def calibrate_mouth_coordinates(landmarks):
    """Keep the speaking range even when playback is cut down to a neutral tail."""
    from talkingface.run_utils import smooth_array, video_pts_process
    from talkingface.utils import main_keypoints_index, INDEX_LIPS_OUTER

    points = landmarks[:, main_keypoints_index, :]
    points = smooth_array(points.reshape(len(points), -1)).reshape(points.shape)
    _, normalized, _, _ = video_pts_process(points)
    lips = normalized[:, INDEX_LIPS_OUTER]
    low, high = lips.min(axis=(0, 1)), lips.max(axis=(0, 1))
    center, extent = (low + high) / 2, (high - low) * .45
    low, high = center - extent, center + extent
    yy, xx = np.mgrid[:100, :150]
    # This is the upstream 100x150 projection grid, calibrated on the full clip.
    grid = np.stack((xx / 149, yy / 100, ((xx - 75) / 75) ** 2,
                     np.ones_like(xx)), axis=-1)
    grid = grid * np.r_[high - low, 1] + np.r_[low, 0]
    return grid.reshape(-1, 4).T


def apply_appearance_references(renderer, avatar_directory, config):
    filename = config.get('reference_file')
    if not filename:
        return
    if Path(filename).name != filename:
        raise ValueError('reference_file must be a filename in the avatar directory')
    with np.load(Path(avatar_directory) / filename, allow_pickle=False) as archive:
        images = archive['images']
        mouth_coords = archive['mouth_coords'] if 'mouth_coords' in archive else None
    if images.shape != (256, 256, 30) or not np.isfinite(images).all():
        raise ValueError('appearance references must contain five valid 256x256 six-channel frames')
    if images.min() < 0 or images.max() > 255:
        raise ValueError('appearance reference pixels must be between 0 and 255')
    network = renderer._RenderModel__net
    device = next(network.parameters()).device
    tensor = torch.from_numpy(images.astype(np.float32) / 255).permute(2, 0, 1).unsqueeze(0).to(device)
    with torch.inference_mode():
        network.ref_input(tensor)
    if mouth_coords is not None:
        if mouth_coords.shape != (4, 15000) or not np.isfinite(mouth_coords).all():
            raise ValueError('mouth coordinates must contain a valid 100x150 homogeneous grid')
        renderer._RenderModel__mouth_coords_array = mouth_coords
