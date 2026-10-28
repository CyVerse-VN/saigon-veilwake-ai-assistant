// DOM/fetch fakes exercise UI lifecycle logic; no browser, microphone or network.
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {webcrypto} from "node:crypto";
import test from "node:test";
import vm from "node:vm";

const appSource = (await readFile(new URL("../../app/web/dev_voice/app.js", import.meta.url), "utf8"))
  .replace(/^import .*;\r?\n/, "");

function harness(fetch, Recorder = class {}) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      value: "", textContent: "", hidden: false, disabled: false, checked: true,
      events: {}, classList: {toggle() {}, add() {}, remove() {}}, style: {setProperty() {}},
      addEventListener(name, callback) {this.events[name] = callback;},
      pause() {}, load() {}, removeAttribute() {}, play: async () => {},
    });
    return nodes.get(id);
  };
  const context = vm.createContext({
    document: {getElementById: node, querySelectorAll: () => []},
    window: {addEventListener() {}}, location: {origin: "http://localhost:8000"},
    BrowserRecorder: Recorder, fetch, URL, AbortController, FormData, crypto: webcrypto,
    setTimeout, clearTimeout, setInterval, clearInterval,
  });
  vm.runInContext(appSource, context);
  return {node, fire: (id, event = "click") => node(id).events[event]({preventDefault() {}})};
}
const json = value => ({ok: true, json: async () => value});
const session = {conversation_id: "offline-session", poll_interval_ms: 1, turn_timeout_seconds: 1};
const result = {status: "COMPLETED", input: {transcript: "Xin chào"}, answer: {text: "Chào bạn!"},
  timings: {}, audio: {status: "NOT_REQUESTED"}};

test("network retry preserves the idempotency key and original text", async () => {
  const keys = [], bodies = [];
  let sends = 0;
  const ui = harness(async (url, options) => {
    assert.equal(options.headers?.Authorization, undefined);
    if (url.pathname.endsWith("/conversations")) return json(session);
    if (options.method === "POST") {
      keys.push(options.headers["Idempotency-Key"]); bodies.push(options.body);
      if (++sends === 1) throw new TypeError("offline-network-test");
      return json({turn_id: "same-turn"});
    }
    return json(result);
  });
  await ui.fire("connect-form", "submit");
  ui.node("message").value = "Xin chào";
  ui.node("want-voice").checked = false;
  await ui.fire("text-form", "submit");
  assert.equal(ui.node("retry-button").hidden, false);
  assert.equal(ui.node("mic-button").disabled, true);
  ui.node("message").value = "Edited after sending";
  await ui.fire("retry-button");
  assert.equal(keys.length, 2); assert.equal(keys[0], keys[1]);
  assert.equal(bodies[0], bodies[1]);
  assert.equal(ui.node("answer").textContent, "Chào bạn!");
  assert.equal(ui.node("mic-button").disabled, false);
});

test("disconnect discards a late model result and prevents a session mix-up", async () => {
  let resolveTurn;
  const waiting = new Promise(resolve => {resolveTurn = resolve;});
  const ui = harness(async (url, options) => {
    if (url.pathname.endsWith("/conversations")) return json(session);
    if (options.method === "POST") return waiting;
    throw new Error("Polling must not happen after disconnect");
  });
  await ui.fire("connect-form", "submit");
  ui.node("message").value = "Xin chào";
  const submitting = ui.fire("text-form", "submit");
  await ui.fire("disconnect-button");
  resolveTurn(json({turn_id: "late-turn"}));
  await submitting;
  assert.equal(ui.node("mic-button").disabled, true);
  assert.equal(ui.node("disconnect-button").hidden, true);
  assert.match(ui.node("status-message").textContent, /Đã ngắt kết nối/);
});

test("disconnect while microphone permission is pending disposes the late recorder", async () => {
  let permit, disposed = 0, requests = 0;
  class FakeRecorder {
    start() {return new Promise(resolve => {permit = resolve;});}
    async dispose() {disposed++;}
  }
  const ui = harness(async (_url, options) => {
    if (options?.method === "POST") requests++;
    return json(session);
  }, FakeRecorder);
  await ui.fire("connect-form", "submit");
  const starting = ui.fire("mic-button");
  await ui.fire("disconnect-button");
  permit(); await starting;
  assert.equal(disposed, 1);
  assert.equal(requests, 1); // Only session creation; no audio submitted.
  assert.equal(ui.node("mic-button").disabled, true);
});

test("story_access_level is sent and RAG verification status + citations render in UI", async () => {
  let postedBody = null;
  const ragResult = {
    status: "COMPLETED",
    input: {transcript: "Người Giữ Im Lặng là ai?"},
    answer: {text: "Người Giữ Im Lặng là thực thể xóa ký ức [ev_01].", verification_status: "PASS"},
    citations: [{evidence_id: "ev_01", title: "Game World", section: "VII. Cốt truyện mùa đầu", span_id: "p-0068"}],
    timings: {llm_seconds: 0.42},
    audio: {status: "NOT_REQUESTED"},
  };
  const ui = harness(async (url, options) => {
    if (url.pathname.endsWith("/conversations")) return json(session);
    if (options?.method === "POST") {
      postedBody = JSON.parse(options.body);
      return json({turn_id: "rag-turn-1"});
    }
    return json(ragResult);
  });
  await ui.fire("connect-form", "submit");
  ui.node("story-access-level").value = "5";
  ui.node("message").value = "Người Giữ Im Lặng là ai?";
  ui.node("want-voice").checked = false;
  await ui.fire("text-form", "submit");
  assert.equal(postedBody.story_access_level, 5);
  assert.equal(ui.node("verification-badge").textContent, "ĐÃ KIỂM CHỨNG · RAG PASS");
  assert.equal(ui.node("citations-area").hidden, false);
  assert.match(ui.node("citations-list").textContent, /\[ev_01\].*p-0068/);
});

test("a retried audio download cannot replace the next turn's audio", async () => {
  let resolveAudio, audioRequests = 0, turns = 0;
  const ui = harness(async (url, options) => {
    if (url.pathname.endsWith("/conversations")) return json(session);
    if (url.pathname.endsWith("/audio")) {
      if (++audioRequests === 1) throw new TypeError("offline-network-test");
      return new Promise(resolve => {resolveAudio = resolve;});
    }
    if (options.method === "POST") return json({turn_id: `turn-${++turns}`});
    return json({...result, audio: turns === 1 ? {status: "READY", endpoint: "/dev/v1/turns/turn-1/audio"} : result.audio});
  });
  await ui.fire("connect-form", "submit");
  ui.node("message").value = "First turn";
  await ui.fire("text-form", "submit");
  assert.equal(ui.node("retry-audio").hidden, false);
  const downloading = ui.fire("retry-audio");
  ui.node("message").value = "Second turn";
  await ui.fire("text-form", "submit");
  resolveAudio({ok: true, blob: async () => new Blob(["offline"])});
  await downloading;
  assert.equal(ui.node("reply-audio").src, undefined);
  assert.equal(ui.node("audio-area").hidden, true);
});
