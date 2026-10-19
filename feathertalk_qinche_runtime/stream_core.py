"""Bounded-history streaming adapter; only received PCM is visible to the encoder.

This approximation preserves released weights. Symmetric TCN and time-wide
GroupNorm mean bounded windows cannot reproduce full-utterance features exactly.
"""
import time
from pathlib import Path
import cv2,numpy as np,torch
from data_utils.feather_hubert.feather_hubert import load_feather_hubert,get_feather_hubert_from_16k_speech
from inference import load_model,prepare_model_input
from face_utils import reshape_audio_feat,gather_audio_window

MODES={'fast':{'lookahead':0,'chunk_ms':40},'balanced':{'lookahead':10,'chunk_ms':80},'quality':{'lookahead':25,'chunk_ms':160}}

class Engine:
    def __init__(self,checkpoint='/work/checkpoints/last.pth',device='cuda',base_image='/work/data/base.jpg',base_landmarks='/work/data/base.lms'):
        torch.set_num_threads(1); self.device=torch.device(device)
        if device=='cuda':torch.cuda.set_per_process_memory_fraction(.45)
        begin=time.perf_counter()
        self.audio=load_feather_hubert('/opt/FeatherTalk/feather_hubert.pth',self.device)
        self.visual=load_model(checkpoint,self.device)
        self.image=cv2.imread(str(base_image))
        self.image_input,self.crop,self.bbox,self.original_size=prepare_model_input(self.image,str(base_landmarks),self.device)
        # Warm up separately from session timing.
        with torch.inference_mode():
            self.visual(self.image_input,torch.zeros(1,40,1024,device=self.device))
            get_feather_hubert_from_16k_speech(np.zeros(16000,dtype=np.float32),self.audio,self.device)
        self.sync();self.load_seconds=time.perf_counter()-begin
    def sync(self):
        if self.device.type=='cuda':torch.cuda.synchronize()
    @torch.inference_mode()
    def encode(self,samples):
        hidden=get_feather_hubert_from_16k_speech(samples,self.audio,self.device)
        return hidden[:(len(hidden)//2)*2].numpy().reshape(-1,2,1024)
