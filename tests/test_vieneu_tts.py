import json
import wave
from pathlib import Path

import httpx
import pytest

from app.integrations.errors import ProviderRequestError
from app.integrations.tts_providers.vieneu_tts import VieneuTtsProvider


def test_tts_requests_pcm_and_writes_playable_wav(tmp_path: Path) -> None:
    pcm = b"\x01\x00\xff\xff" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:8001/v1/audio/speech"
        assert request.headers["authorization"] == "Bearer local-secret"
        payload = json.loads(request.content)
        assert payload == {
            "model": "vieneu-v3-turbo",
            "input": "Xin chào",
            "voice": "Mai Anh",
            "response_format": "pcm",
            "sample_rate": 24000,
            "stream_format": "audio",
        }
        return httpx.Response(200, content=pcm, headers={"X-Sample-Rate": "24000"})

    provider = VieneuTtsProvider(
        "http://127.0.0.1:8001/v1",
        "vieneu-v3-turbo",
        "Mai Anh",
        24000,
        api_key="local-secret",
        transport=httpx.MockTransport(handler),
    )
    output = tmp_path / "reply.wav"

    assert provider.synthesize("Xin chào", output) == output
    with wave.open(str(output), "rb") as wav_file:
        assert (wav_file.getnchannels(), wav_file.getsampwidth(), wav_file.getframerate()) == (
            1,
            2,
            24000,
        )
        assert wav_file.readframes(wav_file.getnframes()) == pcm


def test_tts_works_without_api_key_for_loopback(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"\x00\x00")

    provider = VieneuTtsProvider(
        "http://127.0.0.1:8001/v1",
        "vieneu-v3-turbo",
        "Mai Anh",
        24000,
        transport=httpx.MockTransport(handler),
    )

    assert provider.synthesize("Xin chào", tmp_path / "reply.wav").is_file()


def test_list_voices_uses_the_self_hosted_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:8001/v1/voices"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "Mai Anh", "name": "Mai Anh", "description": "Female · North"}
                ]
            },
        )

    provider = VieneuTtsProvider(
        "http://127.0.0.1:8001/v1",
        "vieneu-v3-turbo",
        "Mai Anh",
        24000,
        transport=httpx.MockTransport(handler),
    )

    assert provider.list_voices() == [
        {"id": "Mai Anh", "name": "Mai Anh", "description": "Female · North"}
    ]


@pytest.mark.parametrize("status", [401, 429, 500])
def test_tts_http_failure_is_safe(tmp_path: Path, status: int) -> None:
    provider = VieneuTtsProvider(
        "http://127.0.0.1:8001/v1",
        "vieneu-v3-turbo",
        "Mai Anh",
        24000,
        api_key="test-secret",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text="test-secret")),
    )

    with pytest.raises(ProviderRequestError, match=f"HTTP {status}") as error:
        provider.synthesize("Xin chào", tmp_path / "reply.wav")
    assert "test-secret" not in str(error.value)


def test_tts_rejects_invalid_pcm(tmp_path: Path) -> None:
    provider = VieneuTtsProvider(
        "http://127.0.0.1:8001/v1",
        "vieneu-v3-turbo",
        "Mai Anh",
        24000,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"bad")),
    )

    with pytest.raises(ProviderRequestError, match="invalid PCM16"):
        provider.synthesize("Xin chào", tmp_path / "reply.wav")
