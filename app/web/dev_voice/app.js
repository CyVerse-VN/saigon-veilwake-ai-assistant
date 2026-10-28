import {BrowserRecorder} from "./audio.js";

const $ = id => document.getElementById(id);
let conversation = null, recording = null, busy = false;
let pending = null, audioUrl = null, timer = null, limitTimer = null, generation = 0;
let pendingAudio = null, audioGeneration = 0;
let abort = new AbortController();
let ttsOptions = null;
const terminal = new Set(["COMPLETED", "COMPLETED_TEXT_ONLY", "FAILED"]);
const states = {
  PENDING: "Đã nhận câu hỏi, đang chờ xử lý…",
  ROUTING: "Đang phân tích câu hỏi…",
  RETRIEVING: "Đang tra cứu tri thức RAG / Web…",
  TRANSCRIBING: "Đang nhận dạng lời nói…",
  GENERATING: "Đang soạn câu trả lời…",
  VERIFYING: "Đang kiểm chứng câu trả lời (Verification Gate)…",
  SYNTHESIZING: "Đang tạo giọng phản hồi (ElevenLabs)…",
  COMPLETED: "Đã hoàn tất lượt trò chuyện.",
  COMPLETED_TEXT_ONLY: "Text đã sẵn sàng. Hiện chưa có giọng phản hồi.",
  FAILED: "Lượt trò chuyện chưa hoàn tất. Bạn có thể thử một lượt mới.",
};

function remoteBase() { return ($("api-base-url")?.value || "").trim().replace(/\/+$/, ""); }
function status(message, error = false) { $("status-message").textContent = message; $("status-message").classList.toggle("error", error); }
function controls() {
  const locked = busy || Boolean(recording) || Boolean(pending);
  $("mic-button").disabled = !conversation || busy || Boolean(pending);
  $("send-text").disabled = !conversation || locked;
  $("want-voice").disabled = locked;
  if ($("api-base-url")) $("api-base-url").disabled = locked;
  $("tts-provider").disabled = locked || !ttsOptions || Boolean(remoteBase());
  $("vieneu-voice").disabled = locked ||
    !ttsOptions || Boolean(remoteBase()) || !["vieneu", "elevenlabs"].includes($("tts-provider").value);
  $("story-access-level").disabled = locked;
  $("mic-label").textContent = recording ? "Dừng và nhận phản hồi" : "Nhấn để nói";
  $("voice-orb").classList.toggle("recording", Boolean(recording));
}
function selectedTts() {
  const provider = $("tts-provider").value || "elevenlabs";
  return {provider, voice: ["vieneu", "elevenlabs"].includes(provider) ? ($("vieneu-voice").value || null) : null};
}
function selectedStoryAccessLevel() {
  const raw = $("story-access-level").value;
  return raw === "" ? null : Number(raw);
}
function updateVoiceChoices() {
  const provider = $("tts-provider").value;
  const voiceLabel = $("vieneu-voice-label"), voice = $("vieneu-voice");
  const option = ttsOptions?.providers?.find(item => item.id === provider);
  const visible = !remoteBase() && ["vieneu", "elevenlabs"].includes(provider) && option?.available && option?.voices?.length > 0;
  voiceLabel.hidden = !visible; voice.hidden = !visible;
  if (visible && typeof voice.replaceChildren === "function") {
    voiceLabel.textContent = provider === "elevenlabs" ? "Giọng ElevenLabs" : "Giọng VieNeu";
    const defaultVoice = provider === "elevenlabs" ? ttsOptions.elevenlabs_default_voice : ttsOptions.vieneu_default_voice;
    const previous = voice.value || defaultVoice;
    voice.replaceChildren(...option.voices.map(item => {
      const element = document.createElement("option");
      element.value = item.id; element.textContent = item.description ? `${item.name} — ${item.description}` : item.name;
      return element;
    }));
    if (previous && [...voice.options].some(item => item.value === previous)) voice.value = previous;
    else if (voice.options.length) voice.value = voice.options[0].value;
  }
  $("tts-choice-note").textContent = remoteBase()
    ? `Đang dùng API Render (${remoteBase()}) với giọng ElevenLabs Voice Lab cấu hình trên server.`
    : (provider === "elevenlabs"
      ? "ElevenLabs (Voice Lab) dùng giọng bạn đã chọn cho lượt kế tiếp."
      : (provider === "vieneu"
        ? "VieNeu chạy local; giọng được áp dụng cho lượt kế tiếp."
        : "Gemini TTS dùng giọng test mặc định của server cho lượt kế tiếp."));
  controls();
}
async function loadTtsOptions() {
  const provider = $("tts-provider");
  if (remoteBase()) {
    ttsOptions = {
      default_provider: "elevenlabs",
      elevenlabs_default_voice: null,
      vieneu_default_voice: null,
      providers: [{id: "elevenlabs", label: "ElevenLabs (Voice Lab · Render)", available: true, voices: []}],
    };
    if (typeof provider.replaceChildren === "function" && typeof document.createElement === "function") {
      const option = document.createElement("option");
      option.value = "elevenlabs";
      option.textContent = "ElevenLabs (Voice Lab · Render)";
      provider.replaceChildren(option);
    }
    provider.value = "elevenlabs";
    updateVoiceChoices();
    return;
  }
  ttsOptions = await (await api("/dev/v1/tts/options")).json();
  const available = ttsOptions.providers.filter(item => item.available);
  if (!available.length) throw new Error("Khong co TTS provider nao san sang de test.");
  provider.replaceChildren(...available.map(item => {
    const option = document.createElement("option"); option.value = item.id; option.textContent = item.label; return option;
  }));
  provider.value = available.some(item => item.id === ttsOptions.default_provider)
    ? ttsOptions.default_provider : available[0].id;
  updateVoiceChoices();
}
async function api(path, options = {}) {
  const base = remoteBase();
  let url;
  if (base) {
    const normalizedPath = path.startsWith("/dev/v1/") ? path.slice(4) : path;
    url = new URL(normalizedPath, base);
    const allowedOrigin = new URL(base).origin;
    if (url.origin !== allowedOrigin || !url.pathname.startsWith("/v1/")) {
      throw new Error("Đường dẫn API không hợp lệ.");
    }
  } else {
    url = new URL(path, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith("/dev/v1/")) {
      throw new Error("Đường dẫn API không hợp lệ.");
    }
  }
  const parent = abort.signal, requestAbort = new AbortController();
  const cancel = () => requestAbort.abort();
  parent.addEventListener("abort", cancel, {once: true});
  const timeout = setTimeout(cancel, base ? 60000 : 30000);
  let response;
  try {
    response = await fetch(url, {...options, signal: requestAbort.signal, cache: "no-store"});
  } catch (error) {
    if (requestAbort.signal.aborted && !parent.aborted) {
      throw new Error("Kết nối quá thời gian chờ. Hãy thử lại cùng lượt này.");
    }
    throw error;
  } finally {
    clearTimeout(timeout); parent.removeEventListener("abort", cancel);
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const error = new Error(payload?.error?.message || `API chưa xử lý được yêu cầu (${response.status}).`);
    error.status = response.status; throw error;
  }
  return response;
}
function clearAudio() {
  audioGeneration++;
  pendingAudio = null; $("retry-audio").hidden = true;
  $("reply-audio").pause(); $("reply-audio").removeAttribute("src"); $("reply-audio").load();
  if (audioUrl) URL.revokeObjectURL(audioUrl);
  audioUrl = null; $("audio-area").hidden = true;
}
function clearResults() {
  clearAudio(); $("retry-button").hidden = true;
  $("transcript").textContent = "Đang chờ nhận dạng…"; $("answer").textContent = "Đang chờ câu trả lời…";
  $("citations-area").hidden = true; $("citations-list").textContent = "";
  $("verification-badge").textContent = "ĐANG KIỂM CHỨNG…";
  for (const id of ["stt-time", "llm-time", "tts-time"]) $(id).textContent = "—";
  for (const step of document.querySelectorAll("[data-stage]")) step.className = "";
}
function showTurn(turn) {
  status(states[turn.status] || "Đang xử lý…", turn.status === "FAILED");
  if (turn.input?.transcript) $("transcript").textContent = turn.input.transcript;
  if (turn.answer) {
    $("answer").textContent = turn.answer.text;
    const vStatus = turn.answer.verification_status;
    $("verification-badge").textContent = vStatus === "PASS"
      ? "ĐÃ KIỂM CHỨNG · RAG PASS"
      : (vStatus === "FALLBACK" ? "GIỚI HẠN / CHẶN SPOILER" : "CHƯA KIỂM CHỨNG");
  }
  const citations = turn.citations?.length ? turn.citations : (turn.answer?.citations || []);
  if (citations.length) {
    $("citations-area").hidden = false;
    $("citations-list").textContent = citations
      .map(c => {
        const kind = c.source_type === "web_search" ? "WEB" : "RAG";
        const sec = Array.isArray(c.section_path) && c.section_path.length ? c.section_path.join(" > ") : (c.section || c.title);
        const span = c.source_span || c.span_id || c.document_version || kind;
        return `[${c.evidence_id}] (${kind}) ${sec} (${span})`;
      })
      .join("\n");
  } else {
    $("citations-area").hidden = true;
  }
  const timings = turn.timings || {};
  for (const [id, value] of [["stt-time", timings.stt_seconds], ["llm-time", timings.llm_seconds], ["tts-time", timings.tts_seconds]]) {
    $(id).textContent = value == null ? "—" : `${value.toFixed(2)}s`;
  }
  const mappedStage = ["ROUTING", "RETRIEVING", "VERIFYING"].includes(turn.status) ? "GENERATING" : turn.status;
  const stages = ["TRANSCRIBING", "GENERATING", "SYNTHESIZING"], position = stages.indexOf(mappedStage);
  for (const step of document.querySelectorAll("[data-stage]")) {
    const index = stages.indexOf(step.dataset.stage);
    step.className = step.dataset.stage === mappedStage ? "active" :
      ((position > index || (terminal.has(turn.status) && turn.status !== "FAILED")) ? "done" : "");
  }
  if (turn.error) status(`${states[turn.status] || ""} ${turn.error.message}`, turn.status === "FAILED");
}
async function playAnswer(turn) {
  if (turn.audio.status !== "READY" || !turn.audio.endpoint) return;
  const current = generation;
  const currentAudio = ++audioGeneration;
  pendingAudio = turn; $("retry-audio").hidden = true;
  try {
    const response = await api(turn.audio.endpoint);
    const blob = await response.blob();
    if (current !== generation || currentAudio !== audioGeneration) return;
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = URL.createObjectURL(blob); pendingAudio = null;
    $("reply-audio").src = audioUrl; $("audio-area").hidden = false;
    $("audio-note").textContent = "Bạn có thể nghe lại. Audio trên server được giữ tạm thời.";
    await $("reply-audio").play().catch(() => {
      $("audio-note").textContent = "Trình duyệt chưa cho tự phát. Nhấn phát để nghe phản hồi.";
    });
  } catch (error) {
    if (current !== generation || currentAudio !== audioGeneration) return;
    if (error.status === 410 || error.status === 404) pendingAudio = null;
    $("retry-audio").hidden = !pendingAudio;
    status(`Text đã sẵn sàng; chưa tải được audio. ${error.message}`, true);
  }
}
async function poll() {
  const current = generation;
  const deadline = Date.now() + ((conversation?.turn_timeout_seconds || 120) + 15) * 1000;
  while (pending?.turnId && current === generation) {
    if (Date.now() > deadline) throw new Error("Đã hết thời gian chờ. Nhấn thử lại để lấy cùng lượt, không gửi câu hỏi mới.");
    const turn = await (await api(`/dev/v1/turns/${pending.turnId}`)).json();
    if (current !== generation) return;
    showTurn(turn);
    if (terminal.has(turn.status)) {
      pending = null; $("retry-button").hidden = true;
      await playAnswer(turn); return;
    }
    await new Promise(resolve => setTimeout(resolve, conversation.poll_interval_ms || 1000));
  }
}
async function submitPending() {
  if (!pending) return;
  const current = generation;
  busy = true; controls(); $("retry-button").hidden = true;
  try {
    if (!pending.turnId) {
      let body, headers = {"Idempotency-Key": pending.key};
      const kind = pending.blob ? "voice-turns" : "turns";
      if (pending.blob) {
        body = new FormData(); body.append("audio", pending.blob, "recording.wav");
        body.append("response_mode", pending.mode); body.append("locale", "vi-VN");
        body.append("tts_provider", pending.tts.provider);
        if (pending.tts.voice) body.append("vieneu_voice", pending.tts.voice);
        if (pending.storyAccessLevel !== null) body.append("story_access_level", String(pending.storyAccessLevel));
      } else {
        headers["Content-Type"] = "application/json";
        const payload = {message: pending.text, response_mode: pending.mode, locale: "vi-VN",
          tts_provider: pending.tts.provider, vieneu_voice: pending.tts.voice};
        if (pending.storyAccessLevel !== null) payload.story_access_level = pending.storyAccessLevel;
        body = JSON.stringify(payload);
      }
      const accepted = await (await api(`/dev/v1/conversations/${conversation.conversation_id}/${kind}`, {method: "POST", headers, body})).json();
      if (current !== generation) return;
      pending.turnId = accepted.turn_id;
    }
    await poll();
  } catch (error) {
    if (current === generation && error.name !== "AbortError") {
      status(error.message || "Kết nối bị gián đoạn. Hãy thử lại.", true);
      if ([400, 401, 403, 404, 413, 415, 422].includes(error.status)) pending = null;
      $("retry-button").hidden = !pending;
    }
  } finally { if (current === generation) { busy = false; controls(); } }
}
async function stopRecording() {
  if (!recording || busy) return;
  const current = generation;
  busy = true; clearInterval(timer); clearTimeout(limitTimer);
  const recorder = recording; recording = null; controls();
  try {
    const blob = await recorder.stop();
    if (current !== generation) return;
    pending = {key: crypto.randomUUID(), blob, mode: $("want-voice").checked ? "text_and_voice" : "text", tts: selectedTts(), storyAccessLevel: selectedStoryAccessLevel()};
    clearResults(); status("Đang tự gửi bản ghi…"); busy = false;
    await submitPending();
  } catch (error) { if (current === generation) status(error.message || "Không thể tạo bản ghi. Hãy thử lại.", true); }
  finally { if (current === generation) { busy = false; $("mic-level").value = 0; controls(); } }
}
$("connect-form").addEventListener("submit", async event => {
  event.preventDefault(); if (busy) return;
  const current = generation;
  busy = true; $("connect-button").disabled = true;
  try {
    const session = await (await api("/dev/v1/conversations", {method: "POST"})).json();
    if (current !== generation) return;
    conversation = session;
    try { await loadTtsOptions(); }
    catch (error) { $("tts-choice-note").textContent = error.message || "Chua tai duoc lua chon TTS."; }
    $("connect-button").hidden = true; $("disconnect-button").hidden = false;
    $("connection-status").textContent = remoteBase() ? "● Đã kết nối (Render Cloud)" : "● Đã kết nối";
    $("connection-status").classList.add("online");
    $("record-hint").textContent = "Nhấn mic để nói. Nhấn lần nữa để dừng và tự gửi.";
    status(remoteBase()
      ? `Đã kết nối tới ${remoteBase()}. Bạn có thể bắt đầu nói hoặc thử bằng văn bản.`
      : "Đã kết nối. Bạn có thể bắt đầu nói hoặc thử bằng văn bản.");
  } catch (error) { if (current === generation) status(error.message || "Chưa kết nối được backend.", true); }
  finally { if (current === generation) { busy = false; $("connect-button").disabled = false; controls(); } }
});
async function disconnectSession() {
  generation++; abort.abort(); abort = new AbortController();
  clearInterval(timer); clearTimeout(limitTimer);
  await recording?.dispose(); recording = null; conversation = null; pending = null; busy = false;
  clearAudio(); $("retry-button").hidden = true;
  $("connect-button").hidden = false; $("disconnect-button").hidden = true;
  $("connection-status").textContent = "Chưa kết nối"; $("connection-status").classList.remove("online");
  status("Đã ngắt kết nối. Lượt đã gửi có thể vẫn đang hoàn tất trên server."); controls();
}
$("disconnect-button").addEventListener("click", disconnectSession);
$("api-base-url")?.addEventListener("change", async () => {
  if (conversation) await disconnectSession();
  status(remoteBase()
    ? `Đã chọn server Render (${remoteBase()}). Nhấn Kết nối để tạo phiên mới.`
    : "Đã chọn Local Dev Server (/dev/v1). Nhấn Kết nối để tạo phiên mới.");
});
$("mic-button").addEventListener("click", async () => {
  if (recording) return stopRecording();
  if (!conversation || busy || pending) return;
  const current = generation;
  busy = true; controls(); status("Đang mở microphone. Hãy cho phép khi trình duyệt hỏi.");
  const recorder = new BrowserRecorder();
  try {
    await recorder.start(level => {
      if (current !== generation) return;
      const visible = Math.min(1, level * 12); $("mic-level").value = visible;
      $("voice-orb").style.setProperty("--level", visible.toString());
    });
    if (current !== generation) { await recorder.dispose(); return; }
    recording = recorder; busy = false; controls();
    status("Đang ghi âm. Nói một câu rồi nhấn dừng; bản ghi sẽ được tự gửi.");
    const started = Date.now(); $("record-time").textContent = "00:00";
    timer = setInterval(() => { $("record-time").textContent = `00:${Math.min(30, Math.floor((Date.now() - started) / 1000)).toString().padStart(2, "0")}`; }, 100);
    limitTimer = setTimeout(stopRecording, 30000);
  } catch (error) {
    if (current !== generation) return;
    busy = false; controls();
    status(error.name === "NotAllowedError" ? "Microphone chưa được cấp quyền. Hãy cho phép microphone cho localhost rồi thử lại." : (error.message || "Không mở được microphone."), true);
  }
});
$("text-form").addEventListener("submit", async event => {
  event.preventDefault(); if (!conversation || busy || pending || recording) return;
  const text = $("message").value.trim(); if (!text) return status("Hãy nhập một câu hỏi trước.", true);
  clearResults(); $("transcript").textContent = text;
  pending = {key: crypto.randomUUID(), text, mode: $("want-voice").checked ? "text_and_voice" : "text", tts: selectedTts(), storyAccessLevel: selectedStoryAccessLevel()};
  status("Đang gửi câu hỏi…"); await submitPending();
});
$("retry-button").addEventListener("click", submitPending);
$("tts-provider").addEventListener("change", updateVoiceChoices);
$("retry-audio").addEventListener("click", async () => {
  if (pendingAudio) await playAnswer(pendingAudio);
});
window.addEventListener("pagehide", () => { abort.abort(); recording?.dispose(); clearInterval(timer); clearTimeout(limitTimer); });
