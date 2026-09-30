"""Cache the same renderer's quiet pose on every frame of an existing avatar."""
import argparse
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np
import torch
from talkingface.audio_model import AudioModel
from talkingface.render_model import RenderModel
from avatar_references import apply_appearance_references
from teeth_restore import TeethRestorer
from idle_animation import rest_mouth
from mouth_clarity import MouthClarity


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--avatar', type=Path, required=True)
    parser.add_argument('--teeth-mode', choices=('model','source'), default='source')
    parser.add_argument('--teeth-strength', type=float, default=1.)
    args = parser.parse_args()
    root = args.avatar
    config_path = root/'avatar_config.json'
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    torch.set_num_threads(2)
    audio, video = AudioModel(), RenderModel()
    audio.loadModel('/checkpoint/audio.pkl')
    video.loadModel('/checkpoint/render.pth')
    video.reset_charactor(str(root/'circle.mp4'),str(root/'keypoint_rotate.pkl'),
                         config.get('render_reference_indices',config['reference_indices']))
    apply_appearance_references(video,root,config)
    teeth = TeethRestorer(root,config,args.teeth_mode,args.teeth_strength)
    clarity = MouthClarity(root,config)
    capture = cv2.VideoCapture(str(root/'circle.mp4'))
    count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    width,height = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    capture.release()
    output = root/'idle_rendered.mp4'
    command = ['ffmpeg','-hide_banner','-loglevel','error','-y','-f','rawvideo',
               '-pix_fmt','bgr24','-s',f'{width}x{height}','-r','25','-i','pipe:0',
               '-an','-c:v','libx264','-crf','16','-preset','veryfast',
               '-pix_fmt','yuv420p','-movflags','+faststart',str(output)]
    encoder = subprocess.Popen(command,stdin=subprocess.PIPE)
    try:
        with torch.inference_mode():
            mouth = rest_mouth(audio)
            np.save(root/'idle_mouth.npy',mouth,allow_pickle=False)
            for index in range(count):
                image = teeth.process(video.interface(mouth),index)
                image = clarity.process(image,index)
                encoder.stdin.write(image.tobytes())
    finally:
        encoder.stdin.close()
        if encoder.wait() != 0:
            raise RuntimeError('idle encoding failed')
    config.update(idle_video='idle_rendered.mp4',idle_mouth='idle_mouth.npy',
                  idle_render={'method':'same full renderer and tooth restoration; stable silent pose',
                               'frames':count,'fps':25,'teeth_mode':args.teeth_mode,
                               'teeth_strength':args.teeth_strength,
                               'mouth_clarity_strength':clarity.strength})
    config_path.write_text(json.dumps(config,indent=2),encoding='utf-8')
    print(json.dumps({'idle':str(output),'frames':count,'teeth':teeth.status(),
                      'mouth_clarity':clarity.status()}),flush=True)


if __name__ == '__main__':
    main()
