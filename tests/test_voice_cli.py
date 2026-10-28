from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integrations.stt_providers import gemini_stt
from scripts import voice_loop


def test_record_only_never_builds_provider_or_reads_keys(
    wav_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_loop, "TEMP_ROOT", tmp_path / "recordings")
    monkeypatch.setattr(voice_loop, "_configure_windows_utf8_console", lambda: None)
    monkeypatch.setattr("sys.argv", ["voice_loop.py", "--record-only"])

    def local_record(path: Path, *args: object) -> None:
        path.write_bytes(wav_path.read_bytes())

    def no_settings() -> None:
        pytest.fail("Offline record-only must not read secrets or build providers")

    monkeypatch.setattr(voice_loop, "record_wav", local_record)
    monkeypatch.setattr(voice_loop, "get_settings", no_settings)
    assert voice_loop.main() == 0
    assert len(list((tmp_path / "recordings").rglob("*.wav"))) == 1


def test_stt_only_reuses_wav_without_microphone_groq_or_tts(
    wav_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_loop, "TEMP_ROOT", tmp_path / "recordings")
    monkeypatch.setattr(voice_loop, "_configure_windows_utf8_console", lambda: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "voice_loop.py",
            "--stt-only",
            "--input-audio",
            str(wav_path),
        ],
    )
    monkeypatch.setattr(
        voice_loop,
        "get_settings",
        lambda: SimpleNamespace(
            gemini_api_key="dummy",
            gemini_stt_model="gemini-3.5-transcribe",
            gemini_request_timeout_seconds=30,
        ),
    )
    requests = []

    def fake_request(*args: object, **kwargs: object) -> dict[str, object]:
        requests.append(args)
        return {
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "Chào"}]}],
        }

    def forbidden(*args: object) -> None:
        pytest.fail("STT-only file replay must not open the mic or build the voice service")

    monkeypatch.setattr(gemini_stt, "post_json", fake_request)
    monkeypatch.setattr(voice_loop, "record_wav", forbidden)
    monkeypatch.setattr(voice_loop, "build_voice_loop_service", forbidden)
    original = wav_path.read_bytes()
    assert voice_loop.main() == 0
    assert wav_path.read_bytes() == original
    assert len(requests) == 1
    assert list((tmp_path / "recordings").rglob("*.wav")) == []
