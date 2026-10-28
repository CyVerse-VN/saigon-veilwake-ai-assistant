import base64
import json
from pathlib import Path

import httpx
import pytest

from app.integrations.errors import ProviderRequestError
from app.integrations.gemini_rest import INTERACTIONS_URL, response_diagnostics
from app.integrations.stt_providers.gemini_stt import GeminiSttProvider, transcript_text


def make_provider(handler: object) -> GeminiSttProvider:
    return GeminiSttProvider(
        "test-secret", "gemini-3.5-transcribe", transport=httpx.MockTransport(handler)
    )


def completed(text: str) -> dict[str, object]:
    return {
        "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}],
    }


def test_stt_sends_exact_wav_and_vietnamese_config(wav_path: Path) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == INTERACTIONS_URL
        assert request.headers["x-goog-api-key"] == "test-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "gemini-3.5-transcribe"
        assert payload["store"] is False
        assert payload["stream"] is False
        assert payload["generation_config"] == {
            "transcription_config": {"language_codes": ["vi-VN"]}
        }
        audio = payload["input"][0]
        assert audio["type"] == "audio"
        assert audio["mime_type"] == "audio/wav"
        assert base64.b64decode(audio["data"]) == wav_path.read_bytes()
        return httpx.Response(200, json=completed("Xin chào"))

    assert make_provider(handler).transcribe(wav_path) == "Xin chào"
    assert len(requests) == 1  # No upload, deletion, polling, or hidden retry.


def test_stt_parses_older_interactions_outputs(wav_path: Path) -> None:
    response = {
        "status": "completed",
        "outputs": [{"type": "text", "text": "Xin "}, {"type": "text", "text": "chào"}],
    }
    provider = make_provider(lambda request: httpx.Response(200, json=response))
    assert provider.transcribe(wav_path) == "Xin chào"


def test_regression_live_transcribe_response_contains_audio_transcription_not_part_text() -> None:
    # Shape observed in the authorized live request; personal audio/transcript not retained here.
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [{"audioTranscription": {"text": "Xin chào"}}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 126, "candidatesTokenCount": 12},
    }
    assert response["candidates"][0]["content"]["parts"][0].get("text") is None
    assert transcript_text(response) == "Xin chào"


def test_structured_transcription_does_not_duplicate_text_or_echo_thoughts() -> None:
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"thought": True, "text": "private reasoning"},
                        {"audioTranscription": {"text": "Xin chào"}, "text": "Xin chào"},
                    ]
                }
            }
        ]
    }
    assert transcript_text(response) == "Xin chào"


def test_legacy_plain_text_response_still_works() -> None:
    assert (
        transcript_text(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Xin "},
                                {"text": "chào"},
                            ]
                        }
                    }
                ]
            }
        )
        == "Xin chào"
    )


def test_empty_response_preserves_safe_provider_evidence(wav_path: Path) -> None:
    response = completed("")
    response["usage"] = {
        "total_input_tokens": 51,
        "total_output_tokens": 0,
        "input_tokens_by_modality": [{"modality": "audio", "tokens": 50}],
        "secret": "test-secret",
    }
    response["debug"] = {"headers": {"authorization": "test-secret"}}
    provider = make_provider(lambda request: httpx.Response(200, json=response))
    with pytest.raises(ProviderRequestError) as error:
        provider.transcribe(wav_path)
    message = str(error.value)
    assert "audio_input_tokens=50" in message
    assert "total_output_tokens=0" in message
    assert "completed without transcript" in message
    assert "test-secret" not in message
    assert "authorization" not in message


@pytest.mark.parametrize("status", ["failed", "cancelled", "incomplete", "in_progress"])
def test_noncompleted_text_is_not_accepted(wav_path: Path, status: str) -> None:
    response = {**completed("must not be used"), "status": status}
    provider = make_provider(lambda request: httpx.Response(200, json=response))
    with pytest.raises(ProviderRequestError, match="did not complete"):
        provider.transcribe(wav_path)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_http_errors_never_expose_provider_body_or_key(wav_path: Path, status: int) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "test-secret"}})

    with pytest.raises(ProviderRequestError, match=f"HTTP {status}") as error:
        make_provider(handler).transcribe(wav_path)
    assert "test-secret" not in str(error.value)
    assert len(calls) == 1


def test_timeout_is_safe_and_not_retried(wav_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("test-secret", request=request)

    with pytest.raises(ProviderRequestError, match="timed out") as error:
        make_provider(handler).transcribe(wav_path)
    assert "test-secret" not in str(error.value)
    assert len(calls) == 1


def test_unknown_status_is_not_echoed() -> None:
    safe = response_diagnostics(
        {
            "status": "test-secret",
            "usage": {"total_output_tokens": "test-secret"},
            "debug": {"secret": "test-secret"},
        }
    )
    assert safe == {"status": "unknown", "output_blocks": 0}


def test_only_final_model_output_is_transcribed(wav_path: Path) -> None:
    response = completed("Xin chào")
    response["steps"].insert(
        0,
        {
            "type": "thought",
            "content": [{"type": "text", "text": "private reasoning"}],
        },
    )
    response["steps"].insert(
        0,
        {
            "type": "user_input",
            "content": [{"type": "text", "text": "input prompt"}],
        },
    )
    provider = make_provider(lambda request: httpx.Response(200, json=response))
    assert provider.transcribe(wav_path) == "Xin chào"
