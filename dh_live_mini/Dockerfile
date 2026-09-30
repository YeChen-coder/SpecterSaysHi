FROM python:3.11-slim
ARG DH_LIVE_REF=4467e97cd97194c2c54762043cf121c6d313db12

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/DH_live \
    && cd /opt/DH_live \
    && git init \
    && git remote add origin https://github.com/kleinlee/DH_live.git \
    && git fetch --depth 1 origin ${DH_LIVE_REF} \
    && git checkout --detach FETCH_HEAD \
    && rm -rf .git \
    && cp -a web_demo/static/assets /opt/seed-assets
LABEL org.opencontainers.image.source="https://github.com/kleinlee/DH_live" \
      org.opencontainers.image.revision="${DH_LIVE_REF}"
RUN pip install --no-cache-dir 'fastapi>=0.115,<1' 'uvicorn>=0.30,<1' 'websockets>=16,<17'

COPY bridge.py /app/bridge.py
COPY start.sh /app/start.sh
COPY SpecterMini.html /opt/DH_live/web_demo/static/SpecterMini.html
COPY specter_mini.js /opt/DH_live/web_demo/static/js/specter_mini.js
COPY MiniLiveIndependent.js specter_independent.js /opt/DH_live/web_demo/static/js/
WORKDIR /opt/DH_live
EXPOSE 8888
CMD ["sh", "/app/start.sh"]
