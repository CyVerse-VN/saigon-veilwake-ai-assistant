"""Gemini TTS adapter that always writes a playable WAV file."""

from __future__ import annotations

import base64
import re
import wave
from io import BytesIO
from pathlib import Path

import httpx

from app.integrations.errors import ProviderRequestError
from app.integrations.gemini_rest import (
    INTERACTIONS_URL,
    output_blocks,
    post_json,
    require_completed,
)


class GeminiTtsProvider:
    """Synthesize one short answer using Gemini's unary TTS interaction API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: int = 30,
        voice: str = "Kore",
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for Gemini TTS.")
        if not model:
            raise ValueError("GEMINI_TTS_MODEL is required for Gemini TTS.")

        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._model = model
        self._voice = voice

    def synthesize(self, text: str, output_path: Path) -> Path:
        try:
            response = post_json(
                self._api_key,
                INTERACTIONS_URL,
                {
                    "model": self._model,
                    "input": text,
                    "response_format": {"type": "audio", "mime_type": "audio/wav"},
                    "generation_config": {"speech_config": [{"voice": self._voice}]},
                    "store": False,
                    "stream": False,
                },
                self._timeout_seconds,
                transport=self._transport,
            )
            require_completed(response)
            output_audio = next(
                block for block in output_blocks(response) if block.get("type") == "audio"
            )
            data = _audio_bytes(output_audio["data"])
            mime_type = output_audio.get("mime_type", "audio/wav")
            _write_wav(output_path, data, mime_type)
        except ProviderRequestError:
            raise
        except Exception as exc:  # Do not reveal provider responses in the terminal.
            raise ProviderRequestError(
                "Gemini TTS failed or timed out. The text response is still available."
            ) from exc
        return output_path


def _audio_bytes(data: bytes | str) -> bytes:
    if isinstance(data, bytes):
        return data
    return base64.b64decode(data, validate=True)


def _write_wav(output_path: Path, data: bytes, mime_type: str) -> None:
    """Save unary WAV directly, or wrap raw 16-bit little-endian PCM in WAV."""
    if data.startswith(b"RIFF"):
        with wave.open(BytesIO(data), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() != 1 or wav.getnframes() == 0:
                raise ValueError("TTS returned an unsupported or empty WAV.")
            if len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2:
                raise ValueError("TTS returned a truncated WAV.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        return

    if mime_type.split(";")[0].lower() not in {"audio/l16", "audio/pcm"}:
        raise ValueError("TTS returned an unsupported audio encoding.")
    if not data or len(data) % 2:
        raise ValueError("TTS returned invalid PCM16 bytes.")
    rate_match = re.search(r"rate=(\d+)", mime_type, flags=re.IGNORECASE)
    sample_rate = int(rate_match.group(1)) if rate_match else 24000
    if not 8000 <= sample_rate <= 48000:
        raise ValueError("TTS returned an unsupported PCM rate.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(data)
