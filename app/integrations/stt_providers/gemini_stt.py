"""Vietnamese transcription via the documented Gemini Interactions REST API.

https://ai.google.dev/gemini-api/docs/transcribe
Short WAVs are sent inline: no Files API upload, processing race, or remote file cleanup.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from app.integrations.errors import ProviderRequestError
from app.integrations.gemini_rest import (
    INTERACTIONS_URL,
    output_blocks,
    post_json,
    require_completed,
    response_diagnostics,
)
from app.modules.voice_loop.audio import inspect_wav, inspect_wav_bytes


class GeminiSttProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: int = 30,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("GEMINI_API_KEY is required for Gemini STT.")
        if not model.strip():
            raise ValueError("GEMINI_STT_MODEL is required for Gemini STT.")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self.last_diagnostics: dict[str, str | int] = {}

    def transcribe(self, audio_path: Path, *, language_code: str = "vi-VN") -> str:
        self.last_diagnostics = {}
        audio_bytes, stats = inspect_wav(audio_path)
        response = post_json(
            self._api_key,
            INTERACTIONS_URL,
            {
                "model": self._model,
                "input": [
                    {
                        "type": "audio",
                        "data": base64.b64encode(audio_bytes).decode("ascii"),
                        "mime_type": "audio/wav",
                    }
                ],
                "generation_config": {"transcription_config": {"language_codes": [language_code]}},
                "store": False,
                "stream": False,
            },
            self._timeout_seconds,
            transport=self._transport,
        )
        self.last_diagnostics = response_diagnostics(response)
        require_completed(response)
        transcript = transcript_text(response)
        if not transcript:
            details = ", ".join(f"{key}={value}" for key, value in self.last_diagnostics.items())
            raise ProviderRequestError(
                "Gemini STT completed without transcript. "
                f"{stats.summary()} | {details}. "
                "Replay the WAV locally to confirm speech. If speech is clear, "
                "the configured model returned no text; changing SDKs will not fix that response."
            )
        return transcript

    def transcribe_bytes(
        self,
        audio_bytes: bytes,
        *,
        mime_type: str = "audio/wav",
        language_code: str = "vi-VN",
    ) -> str:
        """Transcribe raw uploaded audio bytes (WAV, MP3, MP4/M4A, OGG, WebM) for /v1 mobile API."""
        self.last_diagnostics = {}
        normalized_mime = mime_type.split(";")[0].strip().lower() or "audio/wav"
        if normalized_mime in {"audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave"}:
            normalized_mime = "audio/wav"
            if audio_bytes.startswith(b"RIFF") and len(audio_bytes) > 44:
                try:
                    inspect_wav_bytes(audio_bytes)
                except ValueError:
                    pass
        elif normalized_mime in {"audio/m4a", "audio/x-m4a", "audio/aac"}:
            normalized_mime = "audio/mp4"
        elif normalized_mime == "audio/mp3":
            normalized_mime = "audio/mpeg"

        response = post_json(
            self._api_key,
            INTERACTIONS_URL,
            {
                "model": self._model,
                "input": [
                    {
                        "type": "audio",
                        "data": base64.b64encode(audio_bytes).decode("ascii"),
                        "mime_type": normalized_mime,
                    }
                ],
                "generation_config": {"transcription_config": {"language_codes": [language_code]}},
                "store": False,
                "stream": False,
            },
            self._timeout_seconds,
            transport=self._transport,
        )
        self.last_diagnostics = response_diagnostics(response)
        require_completed(response)
        transcript = transcript_text(response)
        if not transcript:
            raise ProviderRequestError("Gemini STT completed without transcript.")
        return transcript


def transcript_text(response: dict[str, Any]) -> str:
    """Read the actual STT output union, including structured AudioTranscription.

    https://ai.google.dev/api/generate-content#AudioTranscription
    Dedicated STT may return Part.audioTranscription.text rather than Part.text.
    """
    blocks = output_blocks(response)
    if blocks:
        return "".join(
            block["text"]
            for block in blocks
            if block.get("type") == "text" and isinstance(block.get("text"), str)
        ).strip()

    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return ""
    content = candidates[0].get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return ""
    chunks = []
    for part in parts:
        if not isinstance(part, dict) or part.get("thought") is True:
            continue
        transcription = part.get("audioTranscription") or part.get("audio_transcription")
        text = transcription.get("text") if isinstance(transcription, dict) else None
        if not isinstance(text, str) or not text.strip():
            text = part.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks).strip()
