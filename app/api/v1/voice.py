from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)

from app.api.v1.conversations import get_turn_service, require_existing_conversation
from app.core.config import Settings, get_settings
from app.core.security import require_internal_service
from app.modules.orchestration.service import InMemoryTurnService
from app.schemas.conversation import ResponseMode, TurnAcceptedResponse

router = APIRouter(tags=["voice"])
ALLOWED_AUDIO_MIME_TYPES = {
    "audio/aac",
    "audio/m4a",
    "audio/mp3",
    "audio/mp4",
    "audio/mpeg",
    "audio/ogg",
    "audio/vnd.wave",
    "audio/wav",
    "audio/wave",
    "audio/webm",
    "audio/x-m4a",
    "audio/x-wav",
}


@router.post(
    "/v1/conversations/{conversation_id}/voice-turns",
    response_model=TurnAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_voice_turn(
    conversation_id: str,
    background_tasks: BackgroundTasks,
    audio: UploadFile = File(...),
    response_mode: ResponseMode = Form(default=ResponseMode.TEXT_AND_VOICE),
    story_access_level: int = Form(default=0, ge=0, le=5),
    locale: str = Form(default="vi-VN"),
    _: None = Depends(require_internal_service),
    settings: Settings = Depends(get_settings),
    service: InMemoryTurnService = Depends(get_turn_service),
) -> TurnAcceptedResponse:
    del locale  # Reserved for STT provider selection.
    require_existing_conversation(service, conversation_id)
    base_mime = (audio.content_type or "").split(";")[0].strip().lower()
    if base_mime not in ALLOWED_AUDIO_MIME_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported audio MIME type.")

    # Read at most max + 1 byte: prevent buffering an unbounded request in development.
    contents = await audio.read(settings.max_audio_bytes + 1)
    if not contents or len(contents) > settings.max_audio_bytes:
        raise HTTPException(
            status_code=413,
            detail="Audio is empty or exceeds the configured size limit.",
        )

    # Do not pretend to transcribe. A real STT adapter will supply this transcript.
    turn_id = service.accept_turn(
        conversation_id,
        "voice",
        None,
        response_mode,
        story_access_level=story_access_level,
        audio_input_bytes=contents,
        audio_input_mime=audio.content_type or "audio/wav",
    )
    background_tasks.add_task(service.process_mock_turn, turn_id)
    return TurnAcceptedResponse(
        turn_id=turn_id, conversation_id=conversation_id, status_url=f"/v1/turns/{turn_id}"
    )
