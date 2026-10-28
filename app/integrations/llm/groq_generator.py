"""Groq adapter for the terminal voice-demo generator."""

from __future__ import annotations

from app.integrations.errors import ProviderRequestError

VOICE_DEMO_SYSTEM_PROMPT = """You are a general voice-demo assistant.
Reply in natural, concise Vietnamese. You do not know Saigon: Veilwake lore, canon, or
game state. Never invent game information. If asked about lore or game state without
verified data, clearly say that you do not have verified information. Do not claim to
have searched systems, databases, or the internet."""


class GroqGeneratorProvider:
    """Generate one concise Vietnamese answer using Groq Chat Completions."""

    def __init__(
        self, api_key: str, model: str, timeout_seconds: int = 20, *, max_retries: int = 2
    ) -> None:
        if not api_key:
            raise ValueError("GROQ_GENERATOR_API_KEY is required for the voice loop.")
        if not model:
            raise ValueError("GROQ_GENERATOR_MODEL is required for the voice loop.")
        try:
            from groq import Groq
        except ImportError as exc:  # pragma: no cover - exercised by the CLI environment
            raise RuntimeError("Install the voice-loop dependencies before using Groq.") from exc
        self._client = Groq(api_key=api_key, timeout=timeout_seconds, max_retries=max_retries)
        self._model = model

    def generate(self, transcript: str) -> str:
        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": VOICE_DEMO_SYSTEM_PROMPT},
                    {"role": "user", "content": transcript},
                ],
                max_completion_tokens=220,
            )
            answer = (completion.choices[0].message.content or "").strip()
        except Exception as exc:  # Do not pass raw SDK/HTTP errors to the terminal.
            raise ProviderRequestError(
                "Groq generation failed or timed out. Check the service/quota and try again."
            ) from exc

        if not answer:
            raise ProviderRequestError("Groq returned an empty response. Please try again.")
        return answer
