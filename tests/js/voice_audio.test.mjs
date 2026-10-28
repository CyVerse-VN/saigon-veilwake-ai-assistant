import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../../app/web/dev_voice/audio.js", import.meta.url), "utf8");
const {resampleMono, encodeWav} = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

test("48k and 44.1k PCM become finite 16k mono samples without changing duration/gain", () => {
  for (const rate of [48000, 44100, 16000, 8000]) {
    const samples = new Float32Array(rate).fill(0.25);
    const converted = resampleMono(samples, rate);
    assert.equal(converted.length, 16000);
    assert.ok(converted.every(value => Number.isFinite(value) && Math.abs(value - 0.25) < 1e-6));
  }
  assert.deepEqual([...resampleMono(new Float32Array([1, -1, 1, -1, 1, -1]), 48000)], [1 / 3, -1 / 3].map(Math.fround));
});

test("WAV header and clipped PCM have the exact little-endian playback format", async () => {
  const blob = encodeWav(new Float32Array([-2, -1, 0, 0.5, 1, 2]));
  const data = new DataView(await blob.arrayBuffer());
  const label = offset => String.fromCharCode(...new Uint8Array(data.buffer, offset, 4));
  assert.equal(blob.type, "audio/wav");
  assert.equal(label(0), "RIFF"); assert.equal(label(8), "WAVE");
  assert.equal(label(12), "fmt "); assert.equal(label(36), "data");
  assert.equal(data.getUint32(4, true), 48);
  assert.equal(data.getUint16(20, true), 1); // PCM
  assert.equal(data.getUint16(22, true), 1); // mono
  assert.equal(data.getUint32(24, true), 16000);
  assert.equal(data.getUint32(28, true), 32000);
  assert.equal(data.getUint16(32, true), 2);
  assert.equal(data.getUint16(34, true), 16);
  assert.equal(data.getUint32(40, true), 12);
  assert.deepEqual(Array.from({length: 6}, (_, i) => data.getInt16(44 + i * 2, true)),
    [-32768, -32768, 0, 16384, 32767, 32767]);
});

test("worklet downmixes stereo and flushes the final partial buffer on stop", async () => {
  let Processor;
  const messages = [];
  const context = vm.createContext({
    AudioWorkletProcessor: class {constructor() {this.port = {postMessage: value => messages.push(value)};}},
    registerProcessor: (name, implementation) => {assert.equal(name, "pcm-recorder"); Processor = implementation;},
  });
  vm.runInContext(await readFile(new URL("../../app/web/dev_voice/pcm-worklet.js", import.meta.url), "utf8"), context);
  const processor = new Processor();
  assert.equal(processor.process([[new Float32Array([1, 0.5]), new Float32Array([-1, 0.5])]]), true);
  assert.equal(messages.length, 0);
  processor.port.onmessage({data: "stop"});
  assert.deepEqual([...messages[0].samples], [0, 0.5]);
  assert.equal(messages[0].rms, Math.sqrt(0.125));
  assert.equal(messages[1].done, true);
  assert.equal(processor.process([]), false);
});
