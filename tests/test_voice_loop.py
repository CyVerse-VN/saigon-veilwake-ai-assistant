from pathlib import Path

import pytest

from app.core.config import Settings
from app.modules.voice_loop.service import (
    VoiceLoopConfigurationError,
    VoiceLoopService,
    build_voice_loop_service,
)


class StubStt:
    def transcribe(self, audio_path: Path, *, language_code: str = "vi-VN") -> str:
        assert audio_path.name == "input.wav"
        assert language_code == "vi-VN"
        return "Xin chào"


class StubGenerator:
    def generate(self, transcript: str) -> str:
        assert transcript == "Xin chào"
        return "Chào bạn, mình có thể giúp gì?"


class StubTts:
    def synthesize(self, text: str, output_path: Path) -> Path:
        output_path.write_bytes(b"RIFFstub")
        return output_path


class FailingTts:
    def synthesize(self, text: str, output_path: Path) -> Path:
        raise TimeoutError("provider timeout")


def test_voice_loop_orchestrates_mocked_providers(tmp_path: Path) -> None:
    result = VoiceLoopService(StubStt(), StubGenerator(), StubTts()).process(
        tmp_path / "input.wav", tmp_path / "reply.wav"
    )

    assert result.transcript == "Xin chào"
    assert result.answer == "Chào bạn, mình có thể giúp gì?"
    assert result.audio_path == tmp_path / "reply.wav"
    assert result.tts_error is None
    assert result.stt_seconds >= 0
    assert result.llm_seconds >= 0


def test_tts_failure_keeps_text_response(tmp_path: Path) -> None:
    result = VoiceLoopService(StubStt(), StubGenerator(), FailingTts()).process(
        tmp_path / "input.wav", tmp_path / "reply.wav"
    )

    assert result.answer == "Chào bạn, mình có thể giúp gì?"
    assert result.audio_path is None
    assert result.tts_error == "TTS failed or timed out; the text response is still available."


def test_missing_required_voice_configuration_is_clear() -> None:
    settings = Settings(
        gemini_api_key="",
        gemini_stt_model="",
        gemini_tts_model="",
        groq_generator_api_key="",
        groq_generator_model="",
    )

    with pytest.raises(VoiceLoopConfigurationError) as error:
        build_voice_loop_service(settings)

    message = str(error.value)
    assert "GEMINI_API_KEY" in message
    assert "GROQ_GENERATOR_MODEL" in message


def test_vieneu_requires_its_local_service_configuration() -> None:
    settings = Settings(
        tts_provider="vieneu",
        gemini_api_key="gemini-key-for-stt",
        groq_generator_api_key="groq-key",
        groq_generator_model="model",
        vieneu_tts_base_url="",
    )

    with pytest.raises(VoiceLoopConfigurationError, match="VIENEU_TTS_BASE_URL"):
        build_voice_loop_service(settings)
