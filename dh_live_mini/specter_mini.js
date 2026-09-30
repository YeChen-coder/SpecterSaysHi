// Feed the mini2.0 model 16 kHz mono WAV, as used by the upstream web demo.
const statusEl = document.getElementById('status');
const sourceBytesPerSecond = 2 * 24000;
const modelBytesPerSecond = 2 * 16000;
// Submit 100 ms of source audio per model segment, retaining 16 kHz WAV input.
const targetBytes = Math.round(sourceBytesPerSecond * 0.1);
let pending = [];
let pendingBytes = 0;
let firstAt = 0;
let lastDelay = null;

function resetAudio() {
  pending = [];
  pendingBytes = 0;
  firstAt = 0;
  if (window.Module && Module._clearAudio) Module._clearAudio();
  statusEl.textContent = '等待 Realtime 音频';
}

function writeWav(pcm) {
  // 24 kHz -> 16 kHz (3 input samples to 2 output samples). Input is low-pass
  // speech from Realtime and the speaker path stays at its original 24 kHz.
  const input = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  const inputSamples = pcm.byteLength / 2;
  const outputSamples = Math.floor(inputSamples * 2 / 3);
  const wav = new Uint8Array(44 + outputSamples * 2);
  const d = new DataView(wav.buffer);
  for (const [offset, name] of [[0, 'RIFF'], [8, 'WAVE'], [12, 'fmt '], [36, 'data']]) {
    for (let i = 0; i < 4; i++) wav[offset + i] = name.charCodeAt(i);
  }
  d.setUint32(4, 36 + outputSamples * 2, true);
  d.setUint32(16, 16, true);
  d.setUint16(20, 1, true);
  d.setUint16(22, 1, true);
  d.setUint32(24, 16000, true);
  d.setUint32(28, modelBytesPerSecond, true);
  d.setUint16(32, 2, true);
  d.setUint16(34, 16, true);
  d.setUint32(40, outputSamples * 2, true);
  for (let i = 0; i < outputSamples; i++) {
    const base = Math.floor(i / 2) * 3;
    const sample = i % 2 === 0
      ? input.getInt16(base * 2, true)
      : Math.round((input.getInt16((base + 1) * 2, true) +
                    input.getInt16((base + 2) * 2, true)) / 2);
    d.setInt16(44 + i * 2, sample, true);
  }
  const ptr = Module._malloc(wav.length);
  Module.HEAPU8.set(wav, ptr);
  Module._setAudioBuffer(ptr, wav.length);
  Module._free(ptr);
}

function flush() {
  if (!pendingBytes || !window.Module || !Module._setAudioBuffer) return;
  const pcm = new Uint8Array(pendingBytes);
  let offset = 0;
  for (const piece of pending) { pcm.set(piece, offset); offset += piece.length; }
  pending = [];
  pendingBytes = 0;
  firstAt = 0;
  writeWav(pcm);
  statusEl.textContent = `Realtime 语音已接入 · 传输 ${Math.round(lastDelay)} ms · 模型音频段 ${Math.round(pcm.length / sourceBytesPerSecond * 1000)} ms`;
}

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${scheme}//${location.host}/view`);
  ws.binaryType = 'arraybuffer';
  ws.onmessage = event => {
    if (typeof event.data === 'string') {
      if (JSON.parse(event.data).type === 'reset') resetAudio();
      return;
    }
    const data = event.data;
    if (data.byteLength < 10) return;
    const sentAt = new DataView(data).getFloat64(0, true);
    lastDelay = Math.max(0, Date.now() - sentAt);
    pending.push(new Uint8Array(data, 8));
    pendingBytes += data.byteLength - 8;
    if (!firstAt) firstAt = performance.now();
    if (pendingBytes >= targetBytes) flush();
  };
  ws.onclose = () => { resetAudio(); setTimeout(connect, 1000); };
}
setInterval(() => { if (firstAt && performance.now() - firstAt > 350) flush(); }, 40);
connect();
