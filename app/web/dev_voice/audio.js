export function resampleMono(input, fromRate, toRate = 16000) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const output = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < output.length; i++) {
    if (ratio < 1) {
      const position = i * ratio, left = Math.floor(position);
      output[i] = input[left] + (input[Math.min(left + 1, input.length - 1)] - input[left]) * (position - left);
    } else {
      // Weighted averaging before downsampling; no naive sample dropping.
      const start = i * ratio, end = (i + 1) * ratio;
      let sum = 0;
      for (let j = Math.floor(start); j < Math.ceil(end); j++) {
        sum += input[j] * (Math.min(end, j + 1) - Math.max(start, j));
      }
      output[i] = sum / ratio;
    }
  }
  return output;
}

export function encodeWav(samples, sampleRate = 16000) {
  const bytes = new ArrayBuffer(44 + samples.length * 2), view = new DataView(bytes);
  const label = (at, text) => [...text].forEach((char, i) => view.setUint8(at + i, char.charCodeAt(0)));
  label(0, "RIFF"); view.setUint32(4, bytes.byteLength - 8, true); label(8, "WAVE");
  label(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  label(36, "data"); view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const value = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true);
  }
  return new Blob([bytes], {type: "audio/wav"});
}

export class BrowserRecorder {
  async start(onLevel) {
    if (!navigator.mediaDevices?.getUserMedia || !globalThis.AudioWorkletNode) {
      throw new Error("Hãy mở bằng localhost/HTTPS trên trình duyệt có hỗ trợ microphone và AudioWorklet.");
    }
    this.chunks = []; this.length = 0;
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true},
      });
      this.context = new AudioContext();
      await this.context.resume();
      await this.context.audioWorklet.addModule(new URL("./pcm-worklet.js", import.meta.url));
      this.node = new AudioWorkletNode(this.context, "pcm-recorder");
      this.node.port.onmessage = ({data}) => {
        if (data.samples) {
          const remaining = Math.max(0, Math.floor(this.context.sampleRate * 30) - this.length);
          const chunk = data.samples.subarray(0, remaining);
          this.chunks.push(chunk); this.length += chunk.length; onLevel(data.rms);
        }
        if (data.done) this.finish?.();
      };
      this.source = this.context.createMediaStreamSource(this.stream);
      this.source.connect(this.node); this.node.connect(this.context.destination);
    } catch (error) { await this.dispose(); throw error; }
  }
  async stop() {
    if (!this.node) throw new Error("Microphone chưa sẵn sàng.");
    try {
      await new Promise(resolve => {
        const timer = setTimeout(resolve, 1000);
        this.finish = () => { clearTimeout(timer); resolve(); };
        this.node.port.postMessage("stop");
      });
      const samples = new Float32Array(this.length);
      let offset = 0;
      for (const chunk of this.chunks) { samples.set(chunk, offset); offset += chunk.length; }
      if (this.length / this.context.sampleRate < 1) {
        throw new Error("Bản ghi quá ngắn. Hãy nói ít nhất một giây.");
      }
      return encodeWav(resampleMono(samples, this.context.sampleRate));
    } finally { await this.dispose(); }
  }
  async dispose() {
    this.stream?.getTracks().forEach(track => track.stop());
    this.source?.disconnect(); this.node?.disconnect();
    if (this.context && this.context.state !== "closed") await this.context.close();
  }
}
