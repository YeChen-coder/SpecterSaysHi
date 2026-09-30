FROM python:3.11-slim
ARG DH_LIVE_FULL_REF=8b74e3f73589339c0e5202ceea5b9a3afb4c3637
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/DH_live && cd /opt/DH_live \
    && git init && git remote add origin https://github.com/kleinlee/DH_live.git \
    && git fetch --depth 1 origin ${DH_LIVE_FULL_REF} \
    && git checkout --detach FETCH_HEAD && rm -rf .git
LABEL org.opencontainers.image.source="https://github.com/kleinlee/DH_live" \
      org.opencontainers.image.revision="${DH_LIVE_FULL_REF}"
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cu124 \
    && pip install --no-cache-dir numpy==1.26.4 opencv-python-headless==4.10.0.84 scipy scikit-learn kaldi-native-fbank fastapi uvicorn websockets
RUN pip install --no-cache-dir tqdm pandas
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libportaudio2 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir numpy==1.26.4 mediapipe==0.10.21 opencv-contrib-python==4.11.0.86
RUN sed -i 's/torch.load(ckpt_path)/torch.load(ckpt_path, map_location=device)/g' /opt/DH_live/talkingface/render_model.py /opt/DH_live/talkingface/audio_model.py
RUN sed -i 's/from model_utils import device/from talkingface.model_utils import device/' /opt/DH_live/talkingface/audio_model.py
COPY prepare_avatar.py prepare_teeth.py teeth_restore.py mouth_clarity.py evaluate_avatar.py avatar_references.py idle_animation.py prepare_idle.py server.py streaming_resampler.py index.html /app/
WORKDIR /opt/DH_live
ENV PYTHONPATH=/opt/DH_live:/app
EXPOSE 8898
CMD ["uvicorn", "server:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "8898"]
