import base64
import json
import wave
from pathlib import Path

import httpx
import pytest

from app.integrations.errors import ProviderRequestError
from app.integrations.tts_providers.gemini_tts import GeminiTtsProvider, _write_wav


def test_tts_parses_current_rest_response_and_preserves_wav(wav_path: Path) -> None:
    original = wav_path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "gemini-3.8-flash-lite-tts"
        assert payload["store"] is False
        assert payload["response_format"]["mime_type"] == "audio/wav"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "audio",
                                "data": base64.b64encode(original).decode("ascii"),
                                "mime_type": "audio/wav",
                            }
                        ],
                    }
                ],
            },
        )

    provider = GeminiTtsProvider(
        "dummy", "gemini-3.8-flash-lite-tts", transport=httpx.MockTransport(handler)
    )
    output = wav_path.with_name("reply.wav")
    assert provider.synthesize("Xin chào", output) == output
    assert output.read_bytes() == original  # Never prepend a second WAV header.


def test_pcm_is_wrapped_with_documented_little_endian_format(tmp_path: Path) -> None:
    output = tmp_path / "reply.wav"
    pcm = b"\x01\x00\xff\xff" * 100
    _write_wav(output, pcm, "audio/L16;codec=pcm;rate=24000")
    with wave.open(str(output), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        assert wav.readframes(wav.getnframes()) == pcm


def test_invalid_audio_is_not_mislabeled_as_pcm(tmp_path: Path) -> None:
    output = tmp_path / "reply.wav"
    with pytest.raises(ValueError):
        _write_wav(output, b"provider error body", "text/plain")
    assert not output.exists()


def test_tts_http_failure_is_safe(tmp_path: Path) -> None:
    provider = GeminiTtsProvider(
        "test-secret",
        "gemini-3.8-flash-lite-tts",
        transport=(httpx.MockTransport(lambda request: httpx.Response(429, text="test-secret"))),
    )
    with pytest.raises(ProviderRequestError, match="HTTP 429") as error:
        provider.synthesize("Xin chào", tmp_path / "reply.wav")
    assert "test-secret" not in str(error.value)
