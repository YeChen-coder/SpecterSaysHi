"""Render identical test audio offline to compare full DH_live avatar references."""
import argparse
import json
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from scipy.io import wavfile

from talkingface.audio_model import AudioModel
from talkingface.render_model import RenderModel
from avatar_references import apply_appearance_references
from teeth_restore import TeethRestorer
from mouth_clarity import MouthClarity


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--avatar', type=Path, required=True)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference-indices', type=int, nargs=5)
    parser.add_argument('--teeth-mode', choices=('model', 'source'), default='model')
    parser.add_argument('--teeth-strength', type=float, default=.85)
    args = parser.parse_args()
    metadata = args.avatar / 'avatar_config.json'
    config = json.loads(metadata.read_text(encoding='utf-8-sig')) if metadata.is_file() else {}
    if config.get('reference_file') and args.reference_indices:
        raise ValueError('Regenerate the separate reference file to change its reference indices')
    indices = args.reference_indices or config.get('render_reference_indices',
                                                 config.get('reference_indices', [0, 75, 150, 225, 300]))
    torch.set_num_threads(2)
    audio = AudioModel()
    audio.loadModel('/checkpoint/audio.pkl')
    video = RenderModel()
    video.loadModel('/checkpoint/render.pth')
    video.reset_charactor(str(args.avatar / 'circle.mp4'),
                         str(args.avatar / 'keypoint_rotate.pkl'), indices)
    apply_appearance_references(video, args.avatar, config)
    teeth = TeethRestorer(args.avatar, config, args.teeth_mode, args.teeth_strength)
    clarity = MouthClarity(args.avatar, config)
    cap = cv2.VideoCapture(str(args.avatar / 'circle.mp4'))
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    rate, samples = wavfile.read(args.audio)
    if rate != 16000 or samples.ndim != 1 or samples.dtype != np.int16:
        raise ValueError('test audio must be mono 16 kHz signed 16-bit PCM WAV')
    samples = samples.astype(np.float32) / 32768
    samples = np.pad(samples, (0, (-len(samples)) % 640))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
               '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{width}x{height}',
               '-r', '25', '-i', 'pipe:0', '-i', str(args.audio),
               '-c:v', 'libx264', '-crf', '17', '-preset', 'veryfast',
               '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
               '-movflags', '+faststart', str(args.output)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    timings = []
    try:
        with torch.inference_mode():
            for frame in range(len(samples) // 640):
                started = time.perf_counter()
                mouth = audio.interface_frame(samples[frame * 640:(frame + 1) * 640])
                # Landmarks have both forward and reversed video halves already.
                video.frame_index %= count
                frame_index = video.frame_index
                image = video.interface(mouth)
                image = teeth.process(image, frame_index)
                image = clarity.process(image, frame_index)
                timings.append((time.perf_counter() - started) * 1000)
                process.stdin.write(image.tobytes())
    finally:
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError('comparison video encoding failed')
    report = {'video': str(args.output), 'teeth': teeth.status(), 'mouth_clarity':clarity.status(), 'reference_indices': config.get('reference_indices', indices)
              if config.get('reference_file') else indices,
              'frames': len(timings), 'avatar_frames': count,
              'mean_inference_ms': round(float(np.mean(timings)), 2),
              'p95_inference_ms': round(float(np.percentile(timings, 95)), 2)}
    args.output.with_suffix('.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
