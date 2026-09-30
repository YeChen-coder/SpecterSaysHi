#!/bin/sh
set -eu
if [ ! -f /opt/DH_live/web_demo/static/assets/combined_data.json.gz ]; then
  cp -a /opt/seed-assets/. /opt/DH_live/web_demo/static/assets/
fi
if [ ! -f /opt/DH_live/web_demo/static/assets/avatar_config.js ]; then
  printf 'CONFIG.chromaKeyEnabled = true;\n' > /opt/DH_live/web_demo/static/assets/avatar_config.js
fi
exec uvicorn bridge:app --app-dir /app --host 0.0.0.0 --port 8888
