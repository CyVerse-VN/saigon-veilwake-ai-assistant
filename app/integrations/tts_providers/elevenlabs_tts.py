"""ElevenLabs (Voice Lab) TTS adapter that writes a playable 16-bit PCM WAV file."""

from __future__ import annotations

import wave
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

from app.integrations.errors import ProviderRequestError

_VOICE_LAB_CATEGORIES = {"cloned", "generated", "professional"}


class ElevenLabsTtsProvider:
    """Synthesize a complete WAV file through the ElevenLabs Text-to-Speech API."""

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model: str = "eleven_flash_v2_5",
        sample_rate: int = 24000,
        timeout_seconds: int = 30,
        *,
        base_url: str = "https://api.elevenlabs.io/v1",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("ELEVENLABS_API_KEY is required for ElevenLabs TTS.")
        if not voice_id or not voice_id.strip():
            raise ValueError("ELEVENLABS_TTS_VOICE_ID is required for ElevenLabs TTS.")
        if not model or not model.strip():
            raise ValueError("ELEVENLABS_TTS_MODEL is required for ElevenLabs TTS.")
        if sample_rate not in {16000, 22050, 24000}:
            raise ValueError("ElevenLabs PCM sample rate must be 16000, 22050, or 24000 Hz.")

        self._api_key = api_key.strip()
        self._voice_id = voice_id.strip()
        self._model = model.strip()
        self._sample_rate = sample_rate
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def list_voices(self) -> list[dict[str, str]]:
        """Return Voice Lab voices when `voices_read` is permitted, or the configured voice ID."""

        default_entry = {
            "id": self._voice_id,
            "name": f"Voice Lab ({self._voice_id[:8]}…)",
            "description": "Giọng mặc định trong .env",
        }
        headers = {"xi-api-key": self._api_key}
        try:
            with httpx.Client(
                timeout=min(self._timeout_seconds, 4), transport=self._transport
            ) as client:
                response = client.get(f"{self._base_url}/voices", headers=headers)
                if response.status_code in {401, 403}:
                    return [default_entry]
                response.raise_for_status()
            raw_voices = response.json().get("voices")
            if not isinstance(raw_voices, list):
                return [default_entry]
            voices: list[dict[str, str]] = []
            seen_ids: set[str] = set()
            sorted_raw = sorted(
                (
                    v
                    for v in raw_voices
                    if isinstance(v, dict) and isinstance(v.get("voice_id"), str)
                ),
                key=lambda v: (
                    0 if v.get("voice_id") == self._voice_id else 1,
                    0 if str(v.get("category") or "").lower() in _VOICE_LAB_CATEGORIES else 1,
                    str(v.get("name") or ""),
                ),
            )
            for item in sorted_raw:
                vid = str(item["voice_id"]).strip()
                if not vid or vid in seen_ids:
                    continue
                seen_ids.add(vid)
                category = str(item.get("category") or "").strip()
                voices.append(
                    {
                        "id": vid,
                        "name": str(item.get("name") or vid),
                        "description": category or "Voice Lab",
                    }
                )
            if self._voice_id not in seen_ids:
                voices.insert(0, default_entry)
            return voices or [default_entry]
        except Exception:
            return [default_entry]

    def synthesize_bytes(self, text: str) -> bytes:
        """Synthesize speech and return a complete 16-bit PCM WAV byte payload in memory."""
        endpoint = (
            f"{self._base_url}/text-to-speech/{self._voice_id}"
            f"?output_format=pcm_{self._sample_rate}"
        )
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "text": text,
            "model_id": self._model,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
            },
        }
        lowered_model = self._model.lower()
        if "v2_5" in lowered_model or "flash" in lowered_model or "turbo" in lowered_model:
            payload["language_code"] = "vi"

        try:
            with httpx.Client(timeout=self._timeout_seconds, transport=self._transport) as client:
                response = client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
            return _pcm16_to_wav_bytes(response.content, self._sample_rate)
        except ProviderRequestError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ProviderRequestError(
                f"ElevenLabs TTS request failed with HTTP {exc.response.status_code}."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderRequestError(
                "ElevenLabs TTS failed or timed out. The text response is still available."
            ) from exc
        except Exception as exc:
            raise ProviderRequestError(
                "ElevenLabs TTS returned invalid audio. The text response is still available."
            ) from exc

    def synthesize(self, text: str, output_path: Path) -> Path:
        wav_bytes = self.synthesize_bytes(text)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(wav_bytes)
        return output_path


def _pcm16_to_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    if not pcm or len(pcm) % 2:
        raise ProviderRequestError("ElevenLabs TTS returned invalid PCM16 audio.")
    buf = BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return buf.getvalue()


def _write_pcm16_wav(output_path: Path, pcm: bytes, sample_rate: int) -> None:
    wav_bytes = _pcm16_to_wav_bytes(pcm, sample_rate)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(wav_bytes)
