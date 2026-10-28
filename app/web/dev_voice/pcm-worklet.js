// No network, no output sound: collect bounded mono PCM in a browser audio thread.
class PcmRecorder extends AudioWorkletProcessor {
  constructor() {
    super(); this.buffer = new Float32Array(2048); this.offset = 0; this.active = true;
    this.port.onmessage = ({data}) => {
      if (data === "stop") {
        this.flush(); this.active = false; this.port.postMessage({done: true});
      }
    };
  }
  flush() {
    if (!this.offset) return;
    const samples = this.buffer.slice(0, this.offset);
    let sum = 0; for (const value of samples) sum += value * value;
    this.port.postMessage({samples, rms: Math.sqrt(sum / samples.length)}, [samples.buffer]);
    this.offset = 0;
  }
  process(inputs) {
    if (!this.active) return false;
    const channels = inputs[0];
    if (!channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let mono = 0;
      for (const channel of channels) mono += channel[i] / channels.length;
      this.buffer[this.offset++] = mono;
      if (this.offset === this.buffer.length) this.flush();
    }
    return true;
  }
}
registerProcessor("pcm-recorder", PcmRecorder);
