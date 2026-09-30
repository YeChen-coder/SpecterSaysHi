"""Extract one clear same-character tooth texture; never replace the idle video."""
import argparse
import json
from pathlib import Path
import cv2
import mediapipe as mp
import numpy as np
from teeth_restore import INNER

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--frame', type=int, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
capture = cv2.VideoCapture(str(args.source))
capture.set(cv2.CAP_PROP_POS_FRAMES, args.frame)
ok, image = capture.read()
capture.release()
if not ok:
    raise ValueError('cannot read the specified donor frame')
height, width = image.shape[:2]
scale = min(1., 640/max(height,width))
with mp.solutions.face_mesh.FaceMesh(static_image_mode=True, max_num_faces=1,
                                    refine_landmarks=True) as mesh:
    result = mesh.process(cv2.cvtColor(cv2.resize(image, (round(width*scale),round(height*scale))), cv2.COLOR_BGR2RGB))
if not result.multi_face_landmarks:
    raise ValueError('cannot find donor mouth landmarks')
points = np.asarray([(p.x*width,p.y*height) for p in result.multi_face_landmarks[0].landmark], np.float32)
x0,y0 = np.maximum(np.floor(points[list(INNER)].min(axis=0)-10).astype(int),0)
x1,y1 = np.minimum(np.ceil(points[list(INNER)].max(axis=0)+11).astype(int),(width,height))
args.output.mkdir(parents=True,exist_ok=True)
np.savez_compressed(args.output/'teeth_texture.npz', image=image[y0:y1,x0:x1], landmarks=points-[x0,y0])
config_path=args.output/'avatar_config.json'
config=json.loads(config_path.read_text(encoding='utf-8-sig'))
config.update(teeth_file='teeth_texture.npz', teeth_source={'video':args.source.name,'frame':args.frame,
             'method':'inner mouth texture; preserve tooth height and generated lip contour'})
config_path.write_text(json.dumps(config,indent=2),encoding='utf-8')
print(json.dumps(config['teeth_source']),flush=True)
