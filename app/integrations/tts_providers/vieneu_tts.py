"""Adapter for a self-hosted VieNeu OpenAI-compatible speech API."""

from __future__ import annotations

import wave
from pathlib import Path

import httpx

from app.integrations.errors import ProviderRequestError


class VieneuTtsProvider:
    """Synthesize a complete WAV file through a private VieNeu service."""

    def __init__(
        self,
        base_url: str,
        model: str,
        voice: str,
        sample_rate: int,
        timeout_seconds: int = 45,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("VIENEU_TTS_BASE_URL is required for VieNeu TTS.")
        if not model.strip():
            raise ValueError("VIENEU_TTS_MODEL is required for VieNeu TTS.")
        if not voice.strip():
            raise ValueError("VIENEU_TTS_VOICE is required for VieNeu TTS.")
        if sample_rate not in {16000, 24000, 48000}:
            raise ValueError("VieNeu TTS sample rate must be 16000, 24000, or 48000 Hz.")

        self._endpoint = base_url.rstrip("/") + "/audio/speech"
        self._model = model
        self._voice = voice
        self._sample_rate = sample_rate
        self._timeout_seconds = timeout_seconds
        self._api_key = api_key.strip() if api_key else None
        self._transport = transport

    def list_voices(self) -> list[dict[str, str]]:
        """Return the preset voices published by the self-hosted service."""
        endpoint = self._endpoint.removesuffix("/audio/speech") + "/voices"
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            with httpx.Client(
                timeout=min(self._timeout_seconds, 3), transport=self._transport
            ) as client:
                response = client.get(endpoint, headers=headers)
                response.raise_for_status()
            data = response.json().get("data")
            if not isinstance(data, list):
                raise ValueError("missing voice list")
            voices = []
            for item in data:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise ValueError("invalid voice entry")
                voices.append(
                    {
                        "id": item["id"],
                        "name": str(item.get("name") or item["id"]),
                        "description": str(item.get("description") or ""),
                    }
                )
            return voices
        except httpx.HTTPError as exc:
            raise ProviderRequestError("VieNeu voices are unavailable.") from exc
        except (TypeError, ValueError) as exc:
            raise ProviderRequestError("VieNeu returned an invalid voice list.") from exc

    def synthesize(self, text: str, output_path: Path) -> Path:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        payload = {
            "model": self._model,
            "input": text,
            "voice": self._voice,
            # VieNeu's streaming WAV header deliberately has an unknown final size.
            # Buffer PCM and write the final WAV locally instead.
            "response_format": "pcm",
            "sample_rate": self._sample_rate,
            "stream_format": "audio",
        }
        try:
            with httpx.Client(timeout=self._timeout_seconds, transport=self._transport) as client:
                response = client.post(self._endpoint, json=payload, headers=headers)
                response.raise_for_status()
            sample_rate = _response_sample_rate(response, self._sample_rate)
            _write_pcm16_wav(output_path, response.content, sample_rate)
        except ProviderRequestError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ProviderRequestError(
                f"VieNeu TTS request failed with HTTP {exc.response.status_code}."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderRequestError(
                "VieNeu TTS failed or timed out. The text response is still available."
            ) from exc
        except Exception as exc:
            raise ProviderRequestError(
                "VieNeu TTS returned invalid audio. The text response is still available."
            ) from exc
        return output_path


def _response_sample_rate(response: httpx.Response, requested_rate: int) -> int:
    value = response.headers.get("X-Sample-Rate")
    if value is None:
        return requested_rate
    try:
        rate = int(value)
    except ValueError as exc:
        raise ProviderRequestError("VieNeu TTS returned an invalid sample rate.") from exc
    if rate not in {8000, 16000, 24000, 48000}:
        raise ProviderRequestError("VieNeu TTS returned an unsupported sample rate.")
    return rate


def _write_pcm16_wav(output_path: Path, pcm: bytes, sample_rate: int) -> None:
    if not pcm or len(pcm) % 2:
        raise ProviderRequestError("VieNeu TTS returned invalid PCM16 audio.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
