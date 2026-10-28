from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings are read from environment or a local, untracked `.env` file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "staging", "production"] = Field(
        default="development", validation_alias=AliasChoices("SVAI_APP_ENV", "APP_ENV", "app_env")
    )
    dev_voice_enabled: bool = False
    dev_voice_audio_ttl_seconds: int = Field(default=600, ge=10, le=3600)
    dev_voice_turn_timeout_seconds: int = Field(default=120, ge=1, le=300)
    dev_voice_session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    dev_voice_max_concurrent_turns: int = Field(default=2, ge=1, le=4)
    dev_voice_max_sessions: int = Field(default=100, ge=1, le=1000)
    dev_voice_max_turns: int = Field(default=500, ge=1, le=5000)
    service_name: str = "saigon-veilwake-ai-guide"
    internal_service_token: str | None = None
    cors_allow_origins: str = Field(
        default="*",
        validation_alias=AliasChoices(
            "SVAI_CORS_ALLOW_ORIGINS", "CORS_ALLOW_ORIGINS", "cors_allow_origins"
        ),
    )
    max_audio_bytes: int = 10 * 1024 * 1024
    max_audio_seconds: int = 90
    groq_generator_api_key: str | None = None
    groq_verifier_api_key: str | None = None
    groq_generator_model: str | None = None
    groq_verifier_model: str | None = None
    groq_api_base_url: str = "https://api.groq.com/openai/v1"
    groq_request_timeout_seconds: int = 20
    groq_max_retries: int = 2
    gemini_api_key: str | None = None
    gemini_stt_model: str = "gemini-3.5-transcribe"
    gemini_tts_model: str = "gemini-3.8-flash-lite-tts"
    gemini_request_timeout_seconds: int = 30
    tts_provider: Literal["gemini", "vieneu", "elevenlabs"] = "gemini"
    vieneu_tts_base_url: str | None = None
    vieneu_tts_api_key: str | None = None
    vieneu_tts_model: str = "vieneu-v3-turbo"
    vieneu_tts_voice: str = "Mai Anh"
    # Environment variables arrive as strings; validate the supported values in the adapter.
    vieneu_tts_sample_rate: int = Field(default=24000, ge=8000, le=48000)
    vieneu_tts_timeout_seconds: int = Field(default=45, ge=1, le=120)
    elevenlabs_api_key: str | None = None
    elevenlabs_tts_voice_id: str | None = None
    elevenlabs_tts_model: str = "eleven_flash_v2_5"
    elevenlabs_tts_sample_rate: int = Field(default=24000, ge=16000, le=24000)
    elevenlabs_tts_timeout_seconds: int = Field(default=30, ge=1, le=120)
    elevenlabs_api_base_url: str = "https://api.elevenlabs.io/v1"
    gemini_embedding_model: str = "gemini-embedding-001"
    gemini_embedding_dimensions: int = 768
    rag_chunk_max_chars: int = 1200
    rag_chunk_overlap_chars: int = 150
    supabase_url: str | None = None
    supabase_secret_key: str | None = None
    supabase_jwks_url: str | None = None
    supabase_publishable_key: str | None = None
    supabase_db_url: str | None = None
    web_search_provider: Literal["wikipedia", "tavily", "brave"] = "wikipedia"
    tavily_api_key: str | None = None
    brave_search_api_key: str | None = None
    web_search_timeout_seconds: int = Field(default=10, ge=1, le=60)
    web_search_cache_ttl_seconds: int = Field(default=900, ge=10, le=86400)

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
