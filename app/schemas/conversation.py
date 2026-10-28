from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ResponseMode(str, Enum):
    TEXT = "text"
    TEXT_AND_VOICE = "text_and_voice"


class TurnStatus(str, Enum):
    PENDING = "PENDING"
    TRANSCRIBING = "TRANSCRIBING"
    ROUTING = "ROUTING"
    RETRIEVING = "RETRIEVING"
    GENERATING = "GENERATING"
    VERIFYING = "VERIFYING"
    SYNTHESIZING = "SYNTHESIZING"
    COMPLETED = "COMPLETED"
    COMPLETED_TEXT_ONLY = "COMPLETED_TEXT_ONLY"
    FAILED = "FAILED"


class AudioStatus(str, Enum):
    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"


class CreateConversationResponse(BaseModel):
    conversation_id: str


class TextTurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)
    npc_id: str | None = Field(default=None, max_length=100)
    location_id: str | None = Field(default=None, max_length=100)
    story_access_level: int = Field(default=0, ge=0, le=5)
    response_mode: ResponseMode = ResponseMode.TEXT_AND_VOICE
    locale: str = Field(default="vi-VN", pattern=r"^[a-z]{2,3}-[A-Z]{2}$")


class TurnAcceptedResponse(BaseModel):
    turn_id: str
    conversation_id: str
    status: TurnStatus = TurnStatus.PENDING
    status_url: str


class TurnInput(BaseModel):
    type: str
    transcript: str | None = None


class Answer(BaseModel):
    answer_id: str
    text: str
    content_mode: str = "FACTUAL_GROUNDED"
    verification_status: str = "PASS"
    citations: list[dict[str, Any]] = Field(default_factory=list)


class Audio(BaseModel):
    status: AudioStatus
    endpoint: str | None = None


class TurnResponse(BaseModel):
    turn_id: str
    conversation_id: str
    status: TurnStatus
    input: TurnInput
    answer: Answer | None = None
    audio: Audio
    request_id: str = Field(default_factory=lambda: f"req_{uuid4().hex}")
