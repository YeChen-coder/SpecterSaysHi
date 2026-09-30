"""Adapt a prepared, frame-aligned reference loop for full DH_live."""
import argparse
import json
import pickle
import shutil
import subprocess
from pathlib import Path

import numpy as np
import cv2

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, default=Path('/mini_data/reference/data'))
parser.add_argument('--output', type=Path, default=Path('/avatar'))
parser.add_argument('--reference-indices', type=int, nargs=5,
                    default=[0, 75, 150, 225, 300])
parser.add_argument('--name', default='legacy-reference')
parser.add_argument('--start-frame', type=int, default=0)
parser.add_argument('--end-frame', type=int)
parser.add_argument('--separate-references', action='store_true',
                    help='Use appearance references from the original clip while cropping its playback segment')
args = parser.parse_args()
source, target = args.source, args.output
target.mkdir(parents=True, exist_ok=True)
with (source / 'processed.pkl').open('rb') as stream:
    forward = pickle.load(stream)
assert forward.shape[1:] == (478, 3)
capture = cv2.VideoCapture(str(source / 'processed.mp4'))
frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
fps = capture.get(cv2.CAP_PROP_FPS)
capture.release()
if frame_count != len(forward) * 2:
    raise ValueError(f'video has {frame_count} frames but requires {len(forward) * 2}')
end = len(forward) if args.end_frame is None else args.end_frame
if not 0 <= args.start_frame < end <= len(forward):
    raise ValueError('playback range must be inside the original forward frames')
selected = forward[args.start_frame:end]
points = np.concatenate((selected, selected[::-1]), axis=0)
reference_limit = len(forward) if args.separate_references else len(points)
if len(set(args.reference_indices)) != 5 or any(
        index < 0 or index >= reference_limit for index in args.reference_indices):
    raise ValueError('reference indices must be five distinct valid video frames')
with (target / 'keypoint_rotate.pkl').open('wb') as stream:
    pickle.dump(points, stream)
if args.start_frame == 0 and end == len(forward):
    shutil.copy2(source / 'processed.mp4', target / 'circle.mp4')
else:
    graph = (f'[0:v]trim=start_frame={args.start_frame}:end_frame={end},'
             'setpts=PTS-STARTPTS,split[a][b];[b]reverse[c];[a][c]concat=n=2:v=1:a=0[v]')
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                    '-i', str(source / 'processed.mp4'), '-filter_complex', graph,
                    '-map', '[v]', '-an', '-r', str(fps), '-c:v', 'libx264',
                    '-crf', '17', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                    str(target / 'circle.mp4')], check=True)
config = {
    'name': args.name, 'reference_indices': args.reference_indices,
    'frames': len(points), 'fps': fps, 'loop': 'forward-reverse',
    'playback_source_range': [args.start_frame, end],
}
if args.separate_references:
    from avatar_references import calibrate_mouth_coordinates
    from talkingface.data.few_shot_dataset import get_ref_images_fromVideo
    from talkingface.utils import main_keypoints_index
    capture = cv2.VideoCapture(str(source / 'processed.mp4'))
    try:
        images = get_ref_images_fromVideo(capture, args.reference_indices,
                                         forward[:, main_keypoints_index, :2])
    finally:
        capture.release()
    if images.shape != (256, 256, 30):
        raise ValueError(f'unexpected appearance reference shape: {images.shape}')
    mouth_coords = calibrate_mouth_coordinates(forward)
    np.savez_compressed(target / 'reference_images.npz', images=images, mouth_coords=mouth_coords)
    config.update(reference_file='reference_images.npz', reference_source_frames=len(forward),
                  mouth_geometry='full original clip including speaking frames',
                  render_reference_indices=np.linspace(0, len(selected) - 1, 5, dtype=int).tolist())
(target / 'avatar_config.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
print(f'full avatar: {len(points)} video and landmark frames', flush=True)
