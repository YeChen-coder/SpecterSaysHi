/* Browser relay for the independent, stateful 24 kHz audio model. */
(() => {
  const status = document.getElementById('status');
  const endpoint = '/inference';
  const sessionId = crypto.randomUUID();
  let bufferMs = 100;
  let chunkBytes = 24000 * 2 * bufferMs / 1000;
  let pending = new Uint8Array(0);
  let chain = Promise.resolve();
  let generation = 0;
  let startAt = 0;
  let latestFrame = -1;
  let lastPacketAt = 0;
  let lastDelay = 0;
  const frames = new Map();
  let visible = new Float32Array(12);
  const zero = new Float32Array(12);

  function queue(path, body) {
    const token = generation;
    chain = chain.catch(() => {}).then(async () => {
      if (token !== generation) return;
      const response = await fetch(endpoint + path, {
        method: 'POST',
        headers: {'Content-Type': 'application/octet-stream',
                  'X-Specter-Session': sessionId},
        body
      });
      if (!response.ok) throw new Error(`inference ${path}: HTTP ${response.status}`);
      if (token !== generation) return;
      if (path === '/audio-push') {
        const result = await response.json();
        for (const item of result.frames) {
          frames.set(item.frame, item.blend);
          latestFrame = Math.max(latestFrame, item.frame);
        }
        status.textContent = `独立模型 · ${bufferMs} ms 音频段 · 传输 ${Math.round(lastDelay)} ms · 控制帧 ${latestFrame + 1}`;
      }
    }).catch(error => {
      status.textContent = '独立模型连接失败：' + error.message;
      console.error(error);
    });
  }

  function reset() {
    generation++;
    pending = new Uint8Array(0);
    frames.clear();
    startAt = 0;
    latestFrame = -1;
    visible = new Float32Array(12);
    status.textContent = '等待 Realtime 音频';
    // Keep the prior request serialised before resetting the model state.
    chain = chain.catch(() => {}).then(async () => {
      const response = await fetch(endpoint + '/audio-reset', {
        method: 'POST', body: new Uint8Array(0),
        headers: {'X-Specter-Session': sessionId}
      });
      if (!response.ok) throw new Error('audio reset failed');
    }).catch(error => {
      status.textContent = '独立模型连接失败：' + error.message;
    });
  }

  function accept(buffer) {
    if (buffer.byteLength <= 8) return;
    const sentAt = new DataView(buffer).getFloat64(0, true);
    lastDelay = Math.max(0, Date.now() - sentAt);
    lastPacketAt = performance.now();
    const input = new Uint8Array(buffer, 8);
    if (!startAt) startAt = lastPacketAt - input.byteLength / 48;
    const joined = new Uint8Array(pending.length + input.length);
    joined.set(pending);
    joined.set(input, pending.length);
    let offset = 0;
    while (joined.length - offset >= chunkBytes) {
      queue('/audio-push', joined.slice(offset, offset + chunkBytes));
      offset += chunkBytes;
    }
    pending = joined.slice(offset);
  }

  window.specterBlendAt = () => {
    if (!startAt || performance.now() - lastPacketAt > 450) {
      visible = zero;
      return visible;
    }
    // Playback is already underway when the relay packet arrives. Use the
    // source clock and the newest available frame to avoid chasing stale audio.
    const target = Math.max(0, Math.floor((performance.now() - startAt) / 40));
    const index = Math.min(target, latestFrame);
    if (index >= 0 && frames.has(index)) {
      const next = new Float32Array(12);
      next.set(frames.get(index), 0);
      visible = next;
      for (const key of frames.keys()) if (key < index - 5) frames.delete(key);
    }
    return visible;
  };

  function connect() {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${scheme}//${location.host}/view`);
    ws.binaryType = 'arraybuffer';
    ws.onmessage = event => {
      if (typeof event.data === 'string') {
        if (JSON.parse(event.data).type === 'reset') reset();
      } else accept(event.data);
    };
    ws.onclose = () => { reset(); setTimeout(connect, 1000); };
  }
  fetch('/settings', {cache: 'no-store'}).then(response => response.json()).then(settings => {
    bufferMs = settings.buffer_ms;
    chunkBytes = 24000 * 2 * bufferMs / 1000;
    reset();
    connect();
  }).catch(error => { status.textContent = '读取缓冲配置失败：' + error.message; });
})();
