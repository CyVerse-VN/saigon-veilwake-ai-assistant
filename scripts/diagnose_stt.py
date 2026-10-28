# ruff: noqa: E402
"""Compare documented Gemini endpoints with one saved WAV and safe metadata only.

Default: one current Interactions STT request. --compare-legacy: two requests total.
Never records audio, calls Groq/TTS, retries, or changes the configured model.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.core.config import get_settings
from app.integrations.errors import ProviderRequestError
from app.integrations.gemini_rest import post_json
from app.integrations.stt_providers.gemini_stt import GeminiSttProvider, transcript_text
from app.modules.voice_loop.audio import inspect_wav


def safe_structure(value: object, depth: int = 0) -> object:
    """Inspect schema and scalar lengths without revealing any response field values."""
    allowed = {
        "candidates",
        "content",
        "parts",
        "text",
        "audioTranscription",
        "audio_transcription",
        "transcript",
        "transcription",
        "words",
        "word",
        "alternatives",
        "segments",
        "result",
        "outputs",
        "steps",
        "output",
        "inlineData",
        "data",
        "type",
        "thought",
        "finishReason",
        "role",
        "annotations",
        "promptFeedback",
        "blockReason",
    }
    if depth > 8:
        return "nested"
    if isinstance(value, dict):
        return {
            key if key in allowed else f"other_field_{i}": safe_structure(item, depth + 1)
            for i, (key, item) in enumerate(value.items())
        }
    if isinstance(value, list):
        return [safe_structure(item, depth + 1) for item in value[:10]]
    if isinstance(value, str):
        return {"string_characters": len(value)}
    return type(value).__name__


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_path", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--compare-legacy", action="store_true", help="Make two STT requests total.")
    mode.add_argument("--legacy-only", action="store_true", help="Make one legacy STT request.")
    args = parser.parse_args()
    try:
        data, stats = inspect_wav(args.audio_path)
        settings = get_settings()
        provider = GeminiSttProvider(
            settings.gemini_api_key or "",
            settings.gemini_stt_model,
            settings.gemini_request_timeout_seconds,
        )
    except (ProviderRequestError, ValueError):
        print("Cannot start: check the input PCM16 WAV and GEMINI_API_KEY/GEMINI_STT_MODEL.")
        return 2
    print(stats.summary())
    if not args.legacy_only:
        print("CURRENT: Interactions REST, inline WAV, vi-VN, store=false")
        try:
            transcript = provider.transcribe(args.audio_path)
            print(f"Transcript characters: {len(transcript)}")
        except (ProviderRequestError, ValueError) as exc:
            print(str(exc))
        print(f"Safe diagnostics: {provider.last_diagnostics}")
    if not (args.compare_legacy or args.legacy_only):
        return 0
    print("LEGACY: generateContent REST, same inline WAV and language hint")
    try:
        response = post_json(
            settings.gemini_api_key or "",
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{settings.gemini_stt_model}:generateContent",
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/wav",
                                    "data": base64.b64encode(data).decode("ascii"),
                                }
                            }
                        ],
                    }
                ],
                "generationConfig": {"audioTranscriptionConfig": {"languageCodes": ["vi-VN"]}},
            },
            settings.gemini_request_timeout_seconds,
        )
        # Do not dump this response: bodies/IDs/URIs/messages are intentionally excluded.
        candidates = response.get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        text_chars = sum(len(p["text"]) for p in parts if isinstance(p.get("text"), str))
        print(f"Old text-only parser characters: {text_chars}")
        print(f"Fixed transcript parser characters: {len(transcript_text(response))}")
        usage = response.get("usageMetadata") or {}
        for field in ("promptTokenCount", "candidatesTokenCount"):
            if type(usage.get(field)) is int:
                print(f"{field}={usage[field]}")
        print("Safe candidate structure: " + json.dumps(safe_structure(candidates)))
    except ProviderRequestError as exc:
        print(str(exc))
    except Exception:
        print("Unexpected legacy response shape; raw response was not logged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
