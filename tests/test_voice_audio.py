import wave
from pathlib import Path

import httpx
import pytest

from app.integrations.stt_providers.gemini_stt import GeminiSttProvider
from app.modules.voice_loop.audio import inspect_wav


def test_inspects_actual_audio_bytes(wav_path: Path) -> None:
    data, stats = inspect_wav(wav_path)
    assert data == wav_path.read_bytes()
    assert stats.seconds == 2
    assert stats.sample_rate == 16000
    assert -16 < stats.rms_dbfs < -14


@pytest.mark.parametrize("seconds", [0.2, 2, 31])
def test_bad_audio_is_rejected_before_provider_call(tmp_path: Path, seconds: float) -> None:
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * int(16000 * seconds))

    def no_network(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid local audio must never reach Gemini")

    provider = GeminiSttProvider(
        "dummy", "gemini-3.5-transcribe", transport=httpx.MockTransport(no_network)
    )
    with pytest.raises(ValueError):
        provider.transcribe(path)


def test_truncated_wav_is_rejected(wav_path: Path) -> None:
    wav_path.write_bytes(wav_path.read_bytes()[:-400])
    with pytest.raises(ValueError, match="truncated"):
        inspect_wav(wav_path)
