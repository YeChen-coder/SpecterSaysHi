FROM pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 libgl1 ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python -m pip install numpy==1.26.4 opencv-python-headless==4.9.0.80 soundfile==0.12.1 librosa==0.11.0 tqdm==4.66.5
RUN python -m pip install transformers==4.39.2
WORKDIR /opt/FeatherTalk
ENV PYTHONPATH=/opt/FeatherTalk OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1
COPY upstream/ /opt/FeatherTalk/
COPY *.py /app/
