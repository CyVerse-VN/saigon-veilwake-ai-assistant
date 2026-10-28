"""Offline API/job tests: all speech and model adapters are deterministic fakes."""

import os
import time
import wave
from array import array
from contextlib import contextmanager
from io import BytesIO
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.modules.voice_loop.dev_api import DevApiError
from app.modules.voice_loop.service import VoiceLoopService

AUTH = {}  # The local demo does not require authentication.
RAW_ERROR = "private-provider-body-and-authorization-sentinel"


def wav_bytes(seconds: int = 1, *, silent: bool = False) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        samples = array("h", [0 if silent else 1000, 0 if silent else -1000])
        audio.writeframes(samples.tobytes() * (8000 * seconds))
    return output.getvalue()


class FakeProviders:
    def __init__(self):
        self.calls = []
        self.tts_selections = []
        self.fail = None
        self.block = None
        self.entered = Event()
        self.release = Event()

    def step(self, name):
        self.calls.append(name)
        if self.block == name:
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError("Offline test gate timed out")
        if self.fail == name:
            raise RuntimeError(RAW_ERROR)

    def transcribe(self, path, *, language_code):
        self.step("stt")
        assert language_code == "vi-VN"
        assert path.read_bytes().startswith(b"RIFF")
        return "Xin chào"

    def generate(self, transcript):
        self.step("llm")
        return "Chào bạn!"

    def synthesize(self, text, output_path):
        self.step("tts")
        output_path.write_bytes(wav_bytes())
        return output_path

    def service(self, provider, voice):
        self.tts_selections.append((provider, voice))
        return VoiceLoopService(self, self, self)


def settings(**changes):
    values = dict(
        _env_file=None,
        app_env="development",
        dev_voice_enabled=True,
        gemini_api_key=None,
        groq_generator_api_key=None,
        groq_generator_model=None,
        groq_verifier_api_key=None,
        internal_service_token=None,
    )
    values.update(changes)
    return Settings(**values)


@contextmanager
def running(tmp_path, **changes):
    providers = FakeProviders()
    root = tmp_path / "dev_audio"
    app = create_app(settings(**changes), dev_service_factory=providers.service, audio_root=root)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        try:
            yield SimpleNamespace(
                client=client,
                providers=providers,
                store=app.state.dev_voice_manager,
                root=root,
            )
        finally:
            providers.release.set()


@pytest.fixture
def dev(tmp_path):
    with running(tmp_path) as instance:
        yield instance


def conversation(dev):
    response = dev.client.post("/dev/v1/conversations", headers=AUTH)
    assert response.status_code == 201
    assert response.json()["mode"] == "voice_demo"
    return response.json()["conversation_id"]


def send_text(
    dev,
    cid,
    *,
    key="offline-key-0001",
    mode="text_and_voice",
    message="Xin chào",
    provider=None,
    voice=None,
):
    body = {"message": message, "response_mode": mode}
    if provider is not None:
        body["tts_provider"] = provider
    if voice is not None:
        body["vieneu_voice"] = voice
    return dev.client.post(
        f"/dev/v1/conversations/{cid}/turns",
        headers={**AUTH, "Idempotency-Key": key},
        json=body,
    )


def finished(dev, turn_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = dev.client.get(f"/dev/v1/turns/{turn_id}", headers=AUTH)
        assert response.status_code == 200
        result = response.json()
        if result["status"] in {"COMPLETED", "COMPLETED_TEXT_ONLY", "FAILED"}:
            return result
        time.sleep(0.01)
    pytest.fail("Offline turn did not finish")


def idle(dev, turn_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with dev.store.lock:
            if not dev.store.jobs[turn_id].running:
                return
        time.sleep(0.01)
    pytest.fail("Offline worker did not release its slot")


def test_voice_round_trip_and_private_audio_cleanup(dev):
    cid = conversation(dev)
    accepted = dev.client.post(
        f"/dev/v1/conversations/{cid}/voice-turns",
        headers={**AUTH, "Idempotency-Key": "voice-key-0001"},
        files={"audio": ("../../untrusted.wav", wav_bytes(), "audio/wav")},
    )
    assert accepted.status_code == 202
    tid = accepted.json()["turn_id"]
    turn = finished(dev, tid)
    idle(dev, tid)
    assert turn["status"] == "COMPLETED"
    assert turn["input"] == {"type": "voice", "transcript": "Xin chào"}
    assert turn["answer"] == {"text": "Chào bạn!", "verification_status": "NOT_RUN"}
    assert dev.providers.calls == ["stt", "llm", "tts"]
    assert all(turn["timings"][key] >= 0 for key in turn["timings"])
    audio = dev.client.get(turn["audio"]["endpoint"], headers=AUTH)
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    assert "no-store" in audio.headers["cache-control"]
    assert audio.content == wav_bytes()
    assert list((dev.root / tid).iterdir()) == [dev.root / tid / "response.wav"]
    assert str(dev.root) not in str(turn)
    assert dev.client.get(turn["audio"]["endpoint"]).status_code == 200


@pytest.mark.parametrize(
    "mode,expected_calls",
    [
        ("text", ["llm"]),
        ("text_and_voice", ["llm", "tts"]),
    ],
)
def test_text_skips_stt_and_respects_audio_mode(dev, mode, expected_calls):
    result = send_text(dev, conversation(dev), mode=mode)
    assert result.status_code == 202
    turn = finished(dev, result.json()["turn_id"])
    assert turn["status"] == "COMPLETED"
    assert turn["timings"]["stt_seconds"] is None
    assert dev.providers.calls == expected_calls
    assert turn["audio"]["status"] == ("READY" if mode == "text_and_voice" else "NOT_REQUESTED")


def test_turn_uses_the_provider_and_vieneu_voice_selected_by_the_test_ui(dev):
    result = send_text(dev, conversation(dev), provider="vieneu", voice="Kim Thanh")
    turn = finished(dev, result.json()["turn_id"])

    assert turn["tts"] == {"provider": "vieneu", "voice": "Kim Thanh"}
    assert dev.providers.tts_selections == [("vieneu", "Kim Thanh")]


def test_turn_passes_story_access_level_and_returns_rag_verification_and_citations(tmp_path):
    from app.modules.voice_loop.service import RagTurnOutput

    class FakeRagProviders(FakeProviders):
        def __init__(self):
            super().__init__()
            self.levels = []

        def generate_with_rag(self, transcript, *, story_access_level=0):
            self.step("llm")
            self.levels.append(story_access_level)
            if story_access_level < 5:
                return RagTurnOutput(
                    text="Thông tin này thuộc phần cốt truyện bạn chưa mở khóa.",
                    verification_status="FALLBACK",
                    citations=(),
                )
            return RagTurnOutput(
                text="Người Giữ Im Lặng là thực thể xóa ký ức [ev_01].",
                verification_status="PASS",
                citations=(
                    {
                        "evidence_id": "ev_01",
                        "title": "Saigon: Veilwake — Game World v1.0",
                        "section": "VII. Cốt truyện mùa đầu",
                        "span_id": "p-0068",
                    },
                ),
            )

    providers = FakeRagProviders()
    root = tmp_path / "dev_audio_rag"
    app = create_app(settings(), dev_service_factory=providers.service, audio_root=root)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        cid = client.post("/dev/v1/conversations").json()["conversation_id"]
        resp_lv0 = client.post(
            f"/dev/v1/conversations/{cid}/turns",
            headers={"Idempotency-Key": "rag-level-0-key"},
            json={
                "message": "Người Giữ Im Lặng là ai?",
                "response_mode": "text",
                "story_access_level": 0,
            },
        )
        assert resp_lv0.status_code == 202
        ns = SimpleNamespace(client=client, store=app.state.dev_voice_manager)
        turn_lv0 = finished(ns, resp_lv0.json()["turn_id"])
        idle(ns, resp_lv0.json()["turn_id"])
        assert turn_lv0["story_access_level"] == 0
        assert turn_lv0["answer"]["verification_status"] == "FALLBACK"
        assert turn_lv0["citations"] == []

        resp_lv5 = client.post(
            f"/dev/v1/conversations/{cid}/turns",
            headers={"Idempotency-Key": "rag-level-5-key"},
            json={
                "message": "Người Giữ Im Lặng là ai?",
                "response_mode": "text",
                "story_access_level": 5,
            },
        )
        assert resp_lv5.status_code == 202
        turn_lv5 = finished(ns, resp_lv5.json()["turn_id"])
        idle(ns, resp_lv5.json()["turn_id"])
        assert turn_lv5["story_access_level"] == 5
        assert turn_lv5["answer"]["verification_status"] == "PASS"
        assert len(turn_lv5["citations"]) == 1
        assert turn_lv5["citations"][0]["span_id"] == "p-0068"
        assert providers.levels == [0, 5]


def test_gemini_rejects_a_vieneu_voice_option(dev):
    response = send_text(dev, conversation(dev), provider="gemini", voice="Kim Thanh")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_TTS_OPTION"


@pytest.mark.parametrize("stage", ["stt", "llm", "tts"])
def test_failures_are_safe_and_tts_preserves_answer(dev, stage):
    dev.providers.fail = stage
    cid = conversation(dev)
    accepted = dev.client.post(
        f"/dev/v1/conversations/{cid}/voice-turns",
        headers={**AUTH, "Idempotency-Key": "failure-key-0001"},
        files={"audio": ("recording.wav", wav_bytes(), "audio/wav")},
    )
    tid = accepted.json()["turn_id"]
    turn = finished(dev, tid)
    idle(dev, tid)
    assert RAW_ERROR not in str(turn)
    assert turn["audio"]["status"] == "UNAVAILABLE"
    if stage == "tts":
        assert turn["status"] == "COMPLETED_TEXT_ONLY"
        assert turn["answer"]["text"] == "Chào bạn!"
        assert turn["error"]["code"] == "TTS_UNAVAILABLE"
    else:
        assert turn["status"] == "FAILED" and turn["answer"] is None
    assert not list(dev.root.rglob("*.wav"))
    assert dev.providers.calls == ["stt", "llm", "tts"][: ["stt", "llm", "tts"].index(stage) + 1]


def test_idempotency_reuses_active_and_completed_turn(dev):
    dev.providers.block = "llm"
    cid = conversation(dev)
    first = send_text(dev, cid)
    assert dev.providers.entered.wait(1)
    again = send_text(dev, cid)
    assert again.status_code == 202
    assert again.json()["turn_id"] == first.json()["turn_id"]
    assert send_text(dev, cid, message="Different").status_code == 409
    assert send_text(dev, cid, key="another-key").status_code == 409
    dev.providers.release.set()
    finished(dev, first.json()["turn_id"])
    assert send_text(dev, cid).json()["turn_id"] == first.json()["turn_id"]
    assert dev.providers.calls == ["llm", "tts"]


@pytest.mark.parametrize("stage", ["llm", "tts"])
def test_watchdog_keeps_slot_until_worker_exits_and_stops_later_stages(tmp_path, stage):
    with running(tmp_path, dev_voice_max_concurrent_turns=1) as dev:
        current = [dev.store.clock()]
        dev.store.clock = lambda: current[0]
        dev.providers.block = stage
        cid, other = conversation(dev), conversation(dev)
        accepted = send_text(dev, cid)
        tid = accepted.json()["turn_id"]
        assert dev.providers.entered.wait(1)
        # Status polling and a second request work while a provider is blocked.
        partial = dev.client.get(f"/dev/v1/turns/{tid}", headers=AUTH).json()
        assert partial["status"] == ("GENERATING" if stage == "llm" else "SYNTHESIZING")
        current[0] += 121
        dev.store.sweep()
        turn = finished(dev, tid)
        assert turn["error"]["code"] == "TURN_TIMEOUT"
        assert turn["status"] == ("FAILED" if stage == "llm" else "COMPLETED_TEXT_ONLY")
        assert send_text(dev, other).status_code == 429
        dev.providers.release.set()
        idle(dev, tid)
        assert "tts" not in dev.providers.calls if stage == "llm" else turn["answer"] is not None
        assert finished(dev, tid) == turn  # A late provider cannot overwrite the watchdog.
        assert not list(dev.root.rglob("*.wav"))


def test_audio_ttl_deletes_file_keeps_text_and_enforces_owner(dev):
    cid = conversation(dev)
    tid = send_text(dev, cid).json()["turn_id"]
    turn = finished(dev, tid)
    idle(dev, tid)
    owner = "local-demo"
    for read in (dev.store.get_turn, dev.store.read_audio):
        with pytest.raises(DevApiError) as error:
            read("another-owner", tid)
        assert error.value.status == 404
    with pytest.raises(DevApiError) as error:
        dev.store.accept("another-owner", cid, "other-key", message="Hi")
    assert error.value.status == 404
    before = dev.store.clock()
    dev.store.clock = lambda: before + 601
    dev.store.sweep()
    assert not list(dev.root.rglob("*.wav"))
    assert dev.client.get(turn["audio"]["endpoint"], headers=AUTH).status_code == 410
    retained = dev.store.get_turn(owner, tid)
    assert retained.audio.status == "EXPIRED" and retained.audio.endpoint is None
    assert retained.answer.text == "Chào bạn!"


@pytest.mark.parametrize(
    "content,mime,status",
    [
        (b"not audio", "audio/wav", 422),
        (wav_bytes(silent=True), "audio/wav", 422),
        (wav_bytes(31), "audio/wav", 422),
        (wav_bytes(), "audio/webm", 415),
        (b"a" * (4 * 1024 * 1024 + 1), "audio/wav", 413),
    ],
    ids=["invalid-header", "silence", "too-long", "wrong-mime", "too-large"],
)
def test_invalid_audio_rejected_before_any_provider(dev, content, mime, status):
    cid = conversation(dev)
    response = dev.client.post(
        f"/dev/v1/conversations/{cid}/voice-turns",
        headers={**AUTH, "Idempotency-Key": "invalid-key-0001"},
        files={"audio": ("recording.wav", content, mime)},
    )
    assert response.status_code == status
    assert dev.providers.calls == []
    assert not list(dev.root.rglob("*.wav"))


def test_chunked_body_limit_before_body_parsing(dev):
    cid = conversation(dev)
    path = f"/dev/v1/conversations/{cid}/turns"
    headers = {**AUTH, "Idempotency-Key": "large-key-0001", "Content-Type": "application/json"}
    response = dev.client.post(path, headers=headers, content=iter([b" " * (5 * 1024 * 1024)]))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "BODY_TOO_LARGE"
    assert dev.client.post(path, content=b"malformed").status_code == 422
    assert dev.providers.calls == []


def test_invalid_fields_missing_key_and_unknown_ids_are_safe(dev):
    cid = conversation(dev)
    path = f"/dev/v1/conversations/{cid}/turns"
    invalid = dev.client.post(path, headers=AUTH, json={"message": RAW_ERROR, "locale": RAW_ERROR})
    assert invalid.status_code == 422
    assert RAW_ERROR not in invalid.text
    assert send_text(dev, cid, message=" ").status_code == 422
    assert send_text(dev, "missing-session").status_code == 404
    assert dev.client.get("/dev/v1/turns/missing", headers=AUTH).status_code == 404
    assert dev.client.get("/dev/v1/turns/missing/audio", headers=AUTH).status_code == 404


@pytest.mark.parametrize(
    "app_env,enabled",
    [
        ("development", False),
        ("production", True),
        ("staging", True),
    ],
)
def test_disabled_outside_explicit_development(tmp_path, app_env, enabled):
    app = create_app(settings(app_env=app_env, dev_voice_enabled=enabled), audio_root=tmp_path)
    with TestClient(app) as client:
        for path in ("/dev/", "/dev/assets/app.js", "/dev/v1/conversations"):
            assert client.get(path, headers=AUTH).status_code == 404


def test_enabled_startup_requires_provider_configuration(tmp_path):
    app = create_app(settings(), audio_root=tmp_path)
    with pytest.raises(ValueError) as error, TestClient(app):
        pass
    assert "GROQ_GENERATOR_MODEL" in str(error.value)


def test_local_ui_assets_headers_and_no_file_picker(dev):
    response = dev.client.get("/dev/")
    assert response.status_code == 200
    assert '<html lang="vi">' in response.text
    assert 'type="file"' not in response.text
    assert "CHƯA KIỂM CHỨNG" in response.text
    assert 'id="access-code"' not in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    for name in ("app.js", "audio.js", "pcm-worklet.js", "style.css", "tts.css"):
        assert dev.client.get(f"/dev/assets/{name}").status_code == 200
    assert dev.client.get("/dev/assets/.env").status_code == 404
    assert dev.client.post("/dev/v1/conversations").status_code == 201


def test_remote_connection_rejected_even_if_dev_is_enabled(tmp_path):
    providers = FakeProviders()
    app = create_app(settings(), dev_service_factory=providers.service, audio_root=tmp_path)
    with TestClient(app, client=("192.0.2.1", 50000)) as client:
        assert client.get("/dev/").status_code == 403
        assert client.post("/dev/v1/conversations").status_code == 403
    assert not providers.calls


def test_bounded_metadata_session_expiry_and_owned_orphan_cleanup(tmp_path):
    with running(tmp_path, dev_voice_max_sessions=1, dev_voice_max_turns=1) as dev:
        cid = conversation(dev)
        assert dev.client.post("/dev/v1/conversations", headers=AUTH).status_code == 429
        tid = send_text(dev, cid, mode="text").json()["turn_id"]
        finished(dev, tid)
        idle(dev, tid)
        assert send_text(dev, cid, key="a-new-key").status_code == 429
        before = dev.store.clock()
        dev.store.clock = lambda: before + 3601
        dev.store.sweep()
        assert dev.client.get(f"/dev/v1/turns/{tid}", headers=AUTH).status_code == 404
        assert not dev.store.keys
        assert send_text(dev, cid).status_code == 404
        assert conversation(dev) != cid
        old = dev.root / ("devturn_" + "a" * 32)
        old.mkdir()
        (old / "response.wav").write_bytes(b"old")
        os.utime(old, (0, 0))
        unrelated = dev.root / "keep-me"
        unrelated.mkdir()
        (unrelated / "response.wav").write_bytes(b"unrelated")
        dev.store.sweep()
        assert not old.exists()
        assert (unrelated / "response.wav").read_bytes() == b"unrelated"


def test_deployment_environment_alias(monkeypatch):
    monkeypatch.setenv("SVAI_APP_ENV", "production")
    assert Settings(_env_file=None).is_development is False


def test_voice_text_only_skips_tts_and_pending_audio_is_not_ready(dev):
    dev.providers.block = "stt"
    cid = conversation(dev)
    accepted = dev.client.post(
        f"/dev/v1/conversations/{cid}/voice-turns",
        headers={**AUTH, "Idempotency-Key": "voice-text-only-key"},
        files={"audio": ("recording.wav", wav_bytes(), "audio/wav")},
        data={"response_mode": "text"},
    )
    assert accepted.status_code == 202
    tid = accepted.json()["turn_id"]
    assert dev.providers.entered.wait(1)
    assert dev.client.get(f"/dev/v1/turns/{tid}/audio", headers=AUTH).status_code == 409
    dev.providers.release.set()
    turn = finished(dev, tid)
    assert turn["status"] == "COMPLETED"
    assert turn["audio"]["status"] == "NOT_REQUESTED"
    assert dev.providers.calls == ["stt", "llm"]


def test_missing_tts_file_preserves_text(dev, monkeypatch):
    def no_file(text, output_path):
        return output_path

    monkeypatch.setattr(dev.providers, "synthesize", no_file)
    tid = send_text(dev, conversation(dev)).json()["turn_id"]
    turn = finished(dev, tid)
    assert turn["status"] == "COMPLETED_TEXT_ONLY"
    assert turn["answer"]["text"] == "Chào bạn!"
    assert turn["error"]["code"] == "TTS_UNAVAILABLE"


def test_shutdown_removes_audio(tmp_path):
    with running(tmp_path) as dev:
        tid = send_text(dev, conversation(dev)).json()["turn_id"]
        finished(dev, tid)
        idle(dev, tid)
        assert list(dev.root.rglob("response.wav"))
    assert not list(dev.root.rglob("*.wav"))


def test_elevenlabs_tts_provider_and_dev_options(tmp_path):
    import httpx

    from app.integrations.tts_providers.elevenlabs_tts import ElevenLabsTtsProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("xi-api-key") == "sk_test_el_key"
        if request.url.path.endswith("/voices"):
            return httpx.Response(401, json={"detail": "missing voices_read permission"})
        assert "/text-to-speech/voice_123" in request.url.path
        assert request.url.params.get("output_format") == "pcm_24000"
        return httpx.Response(200, content=b"\x01\x00\xff\xff" * 1200)

    provider = ElevenLabsTtsProvider(
        api_key="sk_test_el_key",
        voice_id="voice_123",
        model="eleven_flash_v2_5",
        sample_rate=24000,
        transport=httpx.MockTransport(handler),
    )
    voices = provider.list_voices()
    assert len(voices) == 1
    assert voices[0]["id"] == "voice_123"

    out_wav = tmp_path / "el_out.wav"
    provider.synthesize("Xin chào Sài Gòn", out_wav)
    assert out_wav.is_file()
    assert out_wav.read_bytes().startswith(b"RIFF")

    with running(
        tmp_path,
        elevenlabs_api_key="sk_test_el_key",
        elevenlabs_tts_voice_id="voice_123",
    ) as dev:
        opts = dev.client.get("/dev/v1/tts/options").json()
        el_opt = next(p for p in opts["providers"] if p["id"] == "elevenlabs")
        assert el_opt["available"] is True
        assert el_opt["voices"][0]["id"] == "voice_123"

        res = send_text(dev, conversation(dev), provider="elevenlabs", voice="voice_123")
        turn = finished(dev, res.json()["turn_id"])
        assert turn["tts"] == {"provider": "elevenlabs", "voice": "voice_123"}
        assert dev.providers.tts_selections == [("elevenlabs", "voice_123")]
