"""Unverified demo contracts; intentionally independent of Answer's PASS default."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.conversation import ResponseMode

DevStatus = Literal[
    "PENDING",
    "TRANSCRIBING",
    "GENERATING",
    "SYNTHESIZING",
    "COMPLETED",
    "COMPLETED_TEXT_ONLY",
    "FAILED",
]
DevTtsProvider = Literal["gemini", "vieneu", "elevenlabs"]
DevVerificationStatus = Literal["NOT_RUN", "PASS", "FALLBACK"]


class DevTextRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    response_mode: ResponseMode = ResponseMode.TEXT_AND_VOICE
    locale: Literal["vi-VN"] = "vi-VN"
    tts_provider: DevTtsProvider | None = None
    vieneu_voice: str | None = Field(default=None, min_length=1, max_length=80)
    story_access_level: int = Field(default=0, ge=0, le=5)


class DevError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class DevConversation(BaseModel):
    conversation_id: str
    mode: Literal["voice_demo"] = "voice_demo"
    expires_at: datetime
    max_audio_seconds: int = 30
    poll_interval_ms: int = 1000
    turn_timeout_seconds: int


class DevAccepted(BaseModel):
    turn_id: str
    conversation_id: str
    status: DevStatus
    mode: Literal["voice_demo"] = "voice_demo"
    status_url: str


class DevVoiceOption(BaseModel):
    id: str
    name: str
    description: str = ""


class DevTtsProviderOption(BaseModel):
    id: DevTtsProvider
    label: str
    available: bool
    voices: list[DevVoiceOption] = Field(default_factory=list)


class DevTtsOptions(BaseModel):
    default_provider: DevTtsProvider
    vieneu_default_voice: str | None = None
    elevenlabs_default_voice: str | None = None
    providers: list[DevTtsProviderOption]


class DevInput(BaseModel):
    type: Literal["text", "voice"]
    transcript: str | None = None


class DevTtsSelection(BaseModel):
    provider: DevTtsProvider
    voice: str | None = None


class DevAnswer(BaseModel):
    text: str
    verification_status: DevVerificationStatus = "NOT_RUN"


class DevAudio(BaseModel):
    status: Literal["NOT_REQUESTED", "PENDING", "READY", "UNAVAILABLE", "EXPIRED"]
    endpoint: str | None = None
    mime_type: Literal["audio/wav"] = "audio/wav"
    expires_at: datetime | None = None


class DevTimings(BaseModel):
    stt_seconds: float | None = None
    llm_seconds: float | None = None
    tts_seconds: float | None = None


class DevTurn(BaseModel):
    turn_id: str
    conversation_id: str
    mode: Literal["voice_demo"] = "voice_demo"
    status: DevStatus = "PENDING"
    story_access_level: int = 0
    input: DevInput
    tts: DevTtsSelection
    answer: DevAnswer | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    audio: DevAudio
    timings: DevTimings = Field(default_factory=DevTimings)
    error: DevError | None = None
    created_at: datetime
