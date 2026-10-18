FROM feathertalk-lab:local@sha256:66e343888ba5f493a40c2fb95bc8477851049fbed60a4eb510d2cecc39ad6e85
RUN python -m pip install fastapi==0.115.12 uvicorn==0.34.2 websockets==16.0
COPY vendor/ /opt/FeatherTalk/
COPY *.py *.html *.json *.jpg /runtime/
WORKDIR /runtime
ENV PYTHONPATH=/runtime:/opt/FeatherTalk OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1
CMD ["python", "/runtime/server.py"]
